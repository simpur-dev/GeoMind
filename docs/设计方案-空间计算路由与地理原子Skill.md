# 设计方案：空间计算路由 SpatialRouter + 地理原子 Skill（任务卡 1）

> 基线：《GeoMind架构设计文档.md》v1.4。本文所有设计决定都标注对应章节号。
> 上游现状见《开发进度.md》（阶段 0 已 8/8 收口）、《设计方案-演示层与前端地基.md》§8。

## 1. 本轮要关闭的契约缺口（现状盘点，先查后写）

| 架构承诺 | 出处 | 代码现状（实读确认） | 缺口 |
|---|---|---|---|
| L4「空间计算服务：计算路由——能下推的走 OB，OB 不支持的走高德 API；结果归一化为统一 Schema」 | §2.2 L4 | `backend/services/amap_gateway.py` 已完成（六类能力 + 缓存/限流/签名/归一）；**无路由层** | 高德拉取结果没有统一出口，也没有「OB 兜底」的编排点 |
| L3 原子 Skill：地理编码 / 路径规划 / 可达性分析 | §2.2 L3、§11 场景矩阵（POI 检索「低」、可达性分析「低」） | `skills/atomic/` 5 个，全是 SQL 下推型 | 3 个地理能力缺失 |
| §7「等时圈结果的批量筛选：高德出多边形 → OB ST_Contains 批量过滤」 | §7 | `SpatialConstraint.polygon_wkt` 已支持 ST_Contains（`query_builder.py:310`） | 没有产出多边形的上游能力 |
| §9.3 Map Spec kind `isochrone` | §9.3、`api/contracts/map_spec.py:19` | 后端 contracts 已定义；前端仅注册 poi/heatmap 渲染器 | 后端无产出方（渲染器 = 任务卡 2） |

已有可复用件（**不重写**）：`AmapGateway`（`geocode/regeocode/place_around/route_plan/distance_matrix/weather`）、
`backend/api/geo.py` 的 `gcj02_to_wgs84 / wgs84_to_gcj02`、`QueryBuilder` 三层卡点、
`SkillRegistry` 装饰器注册、`ServiceBundle`（`backend/app.py:19` 单一构造点）。

## 2. 范围

**做**：`SpatialRouter`（能力路由 + 坐标归一 + 逐能力降级链）、3 个原子 Skill、QueryBuilder 新增 1 个名称点查模板（扩展）、两份单测、真实 key 一次性冒烟。

**不做**（明确留给后续卡）：
- 不改 `graph.py` 图骨架、不做场景编排（任务卡 5）；
- 不写前端 isochrone 渲染器（任务卡 2）；
- 不改 `amap_gateway.py` 已有方法签名（只在确有需要时加方法——本轮实际**零改动**）；
- 不新增数据表 / migration（§5.6 只允许新增，本轮无需）。

## 3. 关键设计决定

### D1：SpatialRouter 只做两件事——能力路由 + 结果归一（§2.2 L4）

```python
class SpatialRouter:
    def __init__(self, amap: AmapGateway, settings: Settings | None = None,
                 query_builder: QueryBuilder | None = None,
                 place_dict: dict[str, tuple[float, float]] | None = None) -> None: ...
    @property
    def available(self) -> bool: ...                       # = amap.available（无 key 时全走兜底）
    def geocode(self, address: str, city: str = "") -> ResolvedPlace
    def resolve(self, ref: PlaceRef) -> ResolvedPlace      # 地名→geocode；坐标→原样（已是 WGS84）
    def plan_route(self, mode: str, origin: PlaceRef | tuple[float, float],
                   destination: PlaceRef | tuple[float, float]) -> RouteResult
    def isochrone(self, center: PlaceRef | tuple[float, float], minutes: float,
                  mode: str = "driving") -> IsochroneResult
```

- 持有 `AmapGateway + Settings`；**第三个依赖 `query_builder` 只为 geocode 的库内兜底**（可选注入，None 时跳过该级）。
  理由：铁律 1「SQL 只能经 QueryBuilder」+ 铁律 2「Skill.run() 只调 router」——兜底查库必须发生在路由层。
- `place_dict` 默认取 `backend/brain/intent.py` 的 `PLACE_COORDS`（地名→WGS84）。方向上 L4 引用 L2 的词表**数据**可以接受（intent.py 仅 import config，无环），构造参数留口子便于测试与后续换成正式地理编码缓存表。
- 出口结果用 frozen dataclass（与 `CompiledQuery` 同风格）；对外暴露的 Pydantic Schema 由 Skill 层负责（§5.2）。

### D2：坐标系双轨，出口统一 WGS84（§9.6 职责边界 + §7）

高德=GCJ-02，库内=WGS84（§6.3 DDL 铁律）。路由层：
- **主字段一律 WGS84**（可直接进 SQL / Spatial State）；
- **同时带 GCJ-02 副产物**（`polygon_wkt_gcj02`、`polyline_gcj02`），因为 §9.3 Map Spec 契约层坐标定义就是 GCJ-02（`contracts/map_spec.py` 头注）。

这样 §7「高德出多边形 → OB ST_Contains 批量过滤」用 WGS84 WKT，而图层声明用 GCJ-02，两边都不用再猜坐标系。转换只在路由层调用 `backend/api/geo.py` 的纯函数，Skill 层禁止 import 转换函数（铁律 2/3）。

方位角采样、圆近似这类**几何构造**放路由层（属「边界计算」，非「空间裁决」，与 `query_builder.py:314` 的 `_wkt_bbox` 同一口径）；包含/距离判定仍全部下推 OB。

### D3：逐能力降级链（无 key / 网关异常），并在 Skill 层收敛为结构化失败

| 能力 | 在线 | 兜底链 | 全链失败 |
|---|---|---|---|
| geocode | `amap.geocode()`（**必带城市限定**，见下）→ GCJ 转 WGS | ①`PLACE_COORDS` 词表（精确→小写→最长子串）②`dim_poi` name LIKE（QueryBuilder） | `RouteUnavailable("…未定位…")` |
| path_planning | `amap.route_plan()` | **无**（直线近似是空间谎言，不做，§7 路由边界） | `RouteUnavailable` |
| isochrone | `amap.distance_matrix()` 60 点采样 | 无 key 时：`半径 = T × 平均速度` 的 12 顶点正圆近似，`degraded=True` | `RouteUnavailable`（采样后一个合格点都没有时不硬造多边形；有 key 但调用失败也不降级） |

**城市限定是正确性问题，不是优化项**：真实 key 冒烟发现 `geocode("春熙路")` 不带 `city` 时高德按全国匹配，
命中的是外省同名路——实测把 15 分钟等时圈整体甩到凉山（纬度 27.2°），库内候选 0 个。因此 `Settings` 新增
`GEOMIND_DEFAULT_CITY`（默认「成都」，与本库数据范围一致；留空 = 不加过滤），路由层在调用方未给 city 时填入，
已同步进 `.env.example`。

`AmapUnavailable` / `AmapApiError` 一律被路由层捕获，**只向上抛业务级 `RouteUnavailable`**；
Skill 层再把它收敛成「输出对象 + `found=False/ok=False` + `reason`」，因此 **`AmapUnavailable` 不会外泄到图执行**（验收项），
LLM 也不会看到一个异常字符串——它看到的是一个诚实的空结果。

### D4：等时圈 = distance API 方位采样近似（§7「OB 不支持的走高德」+ 高德无等时圈 Web 服务）

- 采样网格：**12 个方位角（每 30°）× 5 档半径（0.5/1/1.5/2/3 km）= 60 个 origin**，单次 `distance_matrix`（高德上限 100，`amap_gateway.py:177` 已拦）。
- 语义偏差如实登记：高德距离测量是「多 origin → 一 destination」，本卡用「采样点→中心」的耗时近似「中心→采样点」的等时边界（非对称误差存在于单行道路网，城区网格道路影响有限）。答辩口径：近似 + 可解释，不宣称精确等时圈。
- 每个方位角取「耗时 ≤ T」的**最远**档；该方位角无任何合格点则退化到 `0.25 km` 并计数 `under_sampled`；任一方位角命中最大半径 3 km 则 `saturated=True`（真实边界可能被采样上限截断）。
- 顶点按方位角升序连成闭合 POLYGON WKT（12 顶点 + 闭合重复点 = 13 对坐标），同时给出 WGS84 与 GCJ-02 两版。
- **type 语义以实测为准**（2026-10-16 真实 key，正北直线 3000 m 的一对起终点）：
  `type=0 → 2999 m / duration 0 s`（**直线距离，不是步行**）、`type=1 → 6089 m / 1284 s`（驾车，17.1 km/h）、
  `type=3 → 3927 m / 3727 s`（步行，3.8 km/h）。网关原注释「1 驾车 0 步行 3 骑行」与实测不符，已改正注释。
  → 等时圈因此**只支持 driving / walking**：高德 `/v3/distance` 没有骑行档，硬套一个错误的 type 是静默错算，
    骑行需求走 `path_planning`（v4 bicycling 接口有）。
- 无 key 降级的正圆速度取名义经验常量（driving 20 / walking 4.0 km·h⁻¹，与上面实测同量级、偏保守），
  **不新增环境变量**——为两个数字造配置面不划算；结果一律 `degraded=True`。
- `under_sampled` 方位角（连最近档都超预算）：半径退化到 250 m 保证多边形闭合，
  但 `duration_s` 回填最近档的**实测耗时**（必然 > 预算），绝不写 0 s——0 会被下游读成「0 秒可达」。

### D5：QueryBuilder 新增名称点查模板（§5.6「只扩展不改」的边界示范）

```python
class PoiNameSpec(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    city: str | None = None
    limit: int = Field(default=5, ge=1, le=50)

def compile_poi_by_name(self, spec: PoiNameSpec) -> CompiledQuery
#   SELECT poi_id, name, address, city, lon, lat, rating
#   FROM dim_poi WHERE status = %(status)s AND name LIKE %(name_like)s [AND city = %(city)s]
#   ORDER BY rating DESC LIMIT %(limit)s
```

- 只新增 Spec + 方法；`compile_hybrid` / `_compile_parts` / `validate` / `dry_run` / `assert_plan` **一行不改**。
- 为什么不用现成的 `compile_hybrid`：它的 dim_poi SELECT 项是 `poi_id, name, category_id, rating`（`query_builder.py:332`），**不含 lon/lat**，地理编码兜底拿不到坐标；而给它加坐标列属于改动既有编译管线（§5.6 冻结项），风险大于收益。
- 走完整三层卡点：`validate`（单语句/只读/列白名单，LIKE 值参数化 `%春熙%`，无字符串拼接）→ `dry_run` → `assert_plan`（期望算子 `TABLE RANGE SCAN | TABLE FULL SCAN`，name 无索引，如实按全表扫断言，不假装索引命中）。

### D6：三个原子 Skill 严格按 §5.2 契约

| Skill 名 | 文件 | 输入 | 输出关键字段 |
|---|---|---|---|
| `geocode` | `skills/atomic/geocode.py` | `address: str`、`city: str = ""` | `found, name, lon, lat`（WGS84）、`gcj02`、`source ∈ amap/place_dict/dim_poi/input`、`poi_id`、`degraded`、`reason`、`evidence`、`plan`、`elapsed_ms` |
| `path_planning` | `skills/atomic/path_planning.py` | `mode ∈ driving/walking/bicycling`、`origin: PlaceRef`、`destination: PlaceRef` | `ok`、`distance_m`、`duration_s`、`polyline`（WGS84）、`polyline_gcj02`、`origin/destination` 解析结果、`degraded`、`reason`、`evidence`、`elapsed_ms` |
| `reachability` | `skills/atomic/reachability.py` | `center: PlaceRef`、`minutes: int (1-120)`、`mode ∈ driving\|walking`（距离 API 无骑行档，见 D4） | `ok`、`polygon_wkt`（WGS84，供 ST_Contains）、`polygon_wkt_gcj02`、`vertices[]`、`saturated`、`under_sampled`、`sampled_points`、`geojson`、`render_spec`、`degraded`、`reason`、`evidence`、`elapsed_ms` |

- `PlaceRef = {name: str | None, lonlat: tuple[float,float] | None}`，二者必给其一（model_validator）——地名与坐标两种调用形态对 MCP/LLM 都友好，JSON Schema 自动导出（`base.py:93 describe()`）。
- `run()` 只调 `ctx.services.router.<cap>()`，零 SQL、零几何（铁律 2）；`elapsed_ms` 用 `time.monotonic()` 墙钟计时。
- `evidence` 取值域沿用 `state/models.py` 的 `Evidence.type ∈ {sql, data, amap, llm}`：在线→`amap`，词表/正圆近似→`data`，dim_poi 兜底→`sql`；note 明写「高德在线」或「库内兜底」，保证可解释（§4.1）。
- `degraded = (source != "amap")`（isochrone 为 `degraded = 正圆近似`）——降级与否由数据来源决定，不由主观判断。
- reachability 的 `render_spec` 用 contracts `LayerSpec` 构造并 `model_dump(mode="json")`（§9.3；铁律 2 之坑：进 SSE 必须 mode="json"）。`source.type="geojson"` + `href=/api/sessions/{sid}/layers/l_iso`；同时把 GeoJSON Polygon 放在输出的 `geojson` 字段——**图层端点服务与前端渲染器属任务卡 2**，本轮先把声明与几何交付到位（如实登记在 §7 遗留）。

### D7：装配走单一构造点

`ServiceBundle` 新增 `router: Any = None`（`services/__init__.py`），`backend/app.py:build_services()` 里
`router=SpatialRouter(amap=gw, settings=s, query_builder=qb)`。测试可注入 fake router/gateway（§5.2 可替换依赖），
不改 Skill 内部。

## 4. 落点文件

| 文件 | 动作 |
|---|---|
| `backend/services/spatial_router.py` | 新建：`RouteUnavailable` / `PlaceRef` / `ResolvedPlace` / `RouteResult` / `IsoVertex` / `IsochroneResult` / `SpatialRouter` |
| `backend/services/__init__.py` | `ServiceBundle` 加 `router` 字段 |
| `backend/app.py` | `build_services` 装配 router |
| `backend/query_builder.py` | 追加 `PoiNameSpec` + `compile_poi_by_name`（纯扩展） |
| `backend/skills/atomic/{geocode,path_planning,reachability}.py` | 新建 3 个原子 Skill（`@register_skill`，discover 自动发现） |
| `tests/test_spatial_router.py` | 新建：MockTransport + fake query_builder，覆盖路由/归一/降级/采样 |
| `tests/test_atomic_geo_skills.py` | 新建：三 Skill 契约 + 注册表/MCP 可见性 + db_ok 兜底对拍 |
| `.env.example` | 新增 `GEOMIND_DEFAULT_CITY`（地理编码城市限定，默认「成都」）占位符与说明（见 D3） |
| `backend/config.py` | 对应新增 `default_city` 字段 |
| `backend/skills/base.py` | 新增模块级 `elapsed_ms_since()`（Skill 共用计时；本机 `time.monotonic` 实测分辨率 15.6 ms，亚一个 tick 全报 0.00，故改用 `perf_counter`，QPC 分辨率 1e-7 s） |

## 5. 实施顺序（每步独立可验证）

1. `spatial_router.py`：先写 geocode + resolve（含词表/库内兜底）→ `python -c` 打印春熙路 WGS/GCJ 对拍。
2. `plan_route` → `isochrone`（采样 + 两种 WKT）→ 手工核对：正圆降级顶点数 12、WKT 13 对闭合。
3. `compile_poi_by_name` → 实 SeekDB 跑三层卡点（`python -c`），确认 dry-run 与 EXPLAIN 断言通过。
4. ServiceBundle/app 装配 → 三个 Skill 文件 → `discover()` 数量 6→9。
5. 两份测试（MockTransport/fake gateway，永不打真实 API）。
6. 质量门：`pytest` + `ruff` + `mypy backend`。
7. 真实 key 冒烟（一次性 `python -c`，不进 pytest）：春熙路→天府广场驾车；春熙路 15 分钟驾车等时圈。
8. 文档同步 + 独立 commit。

## 6. 验收标准（全部可测）

- [x] 无 key：三 Skill 全走降级链并如实打标（`degraded`/`reason`/`evidence`），测试中**无任何异常类型外泄**（`AmapUnavailable` 被路由层收敛）。
- [x] 有 key（MockTransport）：`geocode("春熙路")` 出口为 WGS84，且与 GCJ-02 输入的球面距离落在成都偏移量级 300–600 m。
- [x] `plan_route` 输出 `distance_m/duration_s/polyline`，polyline 为 WGS84 且与 GCJ 输入逐点不同；`mode` 非三条路径之一 → `ValueError`（网关既有校验）。
- [x] `isochrone`：12 顶点（每 30°）+ 闭合 WKT（13 对）；`saturated`/`under_sampled`/`degraded` 三态与构造的假响应一一对应；无 key → 正圆近似且 `degraded=True`。
- [x] `SkillRegistry.instance().describe_all()` 含 `geocode/path_planning/reachability`，每个 `inputSchema.type=="object"`；`pytest tests/test_mcp.py` 全绿（tools/list 为 `>=6` 超集断言，无需改数字）。
- [x] `compile_poi_by_name` 产物过三层卡点（实 SeekDB），且与手写 `SELECT … WHERE name LIKE '%环球中心%'` 对拍 poi_id 一致。
- [x] 全量质量门：pytest 全绿（新增用例数登记于《开发进度.md》）、ruff 0 错、mypy backend 0 错。

## 7. 风险与已知边界（如实登记，不粉饰）

| 项 | 说明 | 处置 |
|---|---|---|
| 等时圈是**近似** | 12×5 网格 + 单向耗时近似 | 输出带 `sampled_points/saturated/under_sampled`，答辩口径「可解释近似」 |
| 3 km 采样上限 | 城区 15 min 驾车可能超出（`saturated=True`） | 如实打标；扩档会超 100 点上限，须拆多次调用（留后续卡） |
| 词表坐标精度 | `PLACE_COORDS` 是人工成都地名表，米级不保证 | 仅作离线兜底，`evidence` 明写「意图词表兜底」 |
| 逆地理词表未覆盖 | 非成都地址在无 key 时必然 miss | 抛 `RouteUnavailable` 带提示，不猜坐标 |
| `l_iso` GeoJSON 端点 | 当前 `routes.get_layer` 只服务 l_candidates/l_heat | 任务卡 2 补渲染器与端点；本轮输出内联 `geojson` 不丢数据 |
| 既有裸 SQL 偏差 | `routes.py:97`、`:105` 两处坐标点查未过 QueryBuilder（历史遗留，本卡未触碰） | 登记为后续收口项（铁律 1 一致性），不在本卡范围内改动 |

## 8. 实施结果（2026-10-16，全部落地）

### 8.1 验收对照

| 项 | 结果 |
|---|---|
| 无 key 三 Skill 全走降级并如实打标 | ✅ 单测钉死：geocode→`place_dict`/`dim_poi`（`degraded=True`+`evidence`），path→`ok=False`+`reason`，iso→正圆 `degraded=True`；`AmapUnavailable` 一层都不外泄（路由层收敛为 `RouteUnavailable`，Skill 层再收敛为结构化结果） |
| geocode 归一 WGS84，偏移量级 | ✅ Mock 104.0817,30.6570 → 出口 104.0792,30.6594（差 372 m，落在 300-600 m 断言带内）；真实 key 春熙路 GCJ(104.078643,30.657952) → WGS(104.076119,30.66036) |
| path_planning 输出 distance/duration/polyline | ✅ Mock 4200 m/780 s/4 点 WGS 折线；真实 key 春熙路→天府广场 **3112 m / 1101 s（18.4 min）/ 75 点**（直线约 1.2 km，路网 3.1 km 合理） |
| reachability 12 顶点 POLYGON + 标记 | ✅ 12 顶点、WKT 13 对且首尾闭合、WGS/GCJ 两版；真实 key 15 min 驾车：命中半径档 [250,500,1000,1500,3000]、`saturated=True`、`under_sampled=2`；30 min 全档 3000 m |
| §7 闭环（多边形下推库内批量筛选） | ✅ 等时圈 WGS WKT 进 `multi_modal_search` → SQL 含 `ST_Contains` + `BETWEEN`（MBR 未破），EXPLAIN `TABLE RANGE SCAN dim_poi(idx_poi_bbox)`，15 min 圈内命中 200 个候选（LIMIT 200 上限） |
| MCP 自动暴露 | ✅ `describe_all()` 9 个（新增 3 个均 `type=object` + properties）；`PlaceRef` 嵌套模型进 `$defs`；`tests/test_mcp.py` 全绿 |
| QueryBuilder 扩展过三层卡点 | ✅ `compile_poi_by_name` 实 SeekDB：validate/dry-run/EXPLAIN 全过（`TABLE FULL SCAN`，2 万行毫秒级），LIKE 通配符转义、值全参数化 |
| 质量门 | ✅ pytest **123 passed**（本卡新增 49：路由 27 + Skill 22）、ruff 0 错、mypy 42 文件 0 错 |
| 断言可信度 | ✅ 变异检查 **11/11 全部被抓住**（不转换坐标 / saturated 恒 False / 退化半径改档 / 高德异常外泄 / 速度换算错 / WKT 不闭合 / LIKE 不转义 / degraded 写死 / under_sampled 耗时退回 0 s / 不带城市 / 等时圈允许骑行），基线与还原后均全绿 |

### 8.2 真实 key 冒烟抓出的两个坑（离线单测抓不到，都是根因级）

1. **地理编码不带城市 → 跨省误命中同名路**。第一轮冒烟里 `春熙路` 被解析到纬度 27.2°（凉山一带），
   15 分钟等时圈整个甩出成都、库内候选 0 个；`path_planning` 也给出 492 km 的荒唐路线。
   修法：`GEOMIND_DEFAULT_CITY`（默认成都）在路由层缺省注入 `city`，并加断言钉住（变异「不带城市」会变红）。
2. **高德 `/v3/distance` 的 type 语义与注释相反**。原注释「1 驾车 0 步行 3 骑行」；实测 3000 m 正北起终点：
   `0 → 2999 m / duration 0 s`（直线）、`1 → 6089 m / 1284 s`（驾车）、`3 → 3927 m / 3727 s`（步行）。
   用 0 当步行时，每个采样点耗时恒 0 →「步行 15 分钟」推出 3 km 且每档都命中，是彻底的错算。
   修法：`{driving:1, walking:3}` + 等时圈**取消骑行**（无对应 type，不静默套用）+ Skill Schema 同步收窄 +
   网关注释按实测改正（签名未动）。
3. 附带修：`under_sampled` 方位角的 `duration_s` 原写 0.0，会被下游读成「0 秒可达」→ 改为回填最近档实测耗时。

### 8.3 已知边界（如实登记）

- 等时圈是 **12×5 网格近似**，3 km 采样上限会截断（`saturated=True` 即信号）；扩档需拆多次 distance 调用（留后续卡）。
- 高德 distance 只支持「多 origin → 一 destination」，本卡用「采样点→中心」耗时近似「中心→采样点」等时边界。
- 词表坐标是人工成都地名表（米级不保证），仅作无 key 兜底，`evidence` 明写来源。
- `l_iso` 的 GeoJSON 端点与前端 isochrone 渲染器同为**任务卡 2** 范围；本轮输出已带内联 `geojson`，不丢数据。
- `routes.py` 两处坐标点查仍是裸 SQL（历史遗留，本卡未触碰），登记为铁律 1 一致性收口项。
