# -*- coding: utf-8 -*-
"""
GeoMind 生产库样例数据生成器（成都主城）

设计要点
--------
1. 坐标：成都主城 WGS84 经纬度；DDL 一律 SRID 0（SeekDB 不支持 SRID 4326）。
   分布为「8 个商圈中心高斯聚集 70% + 全域均匀 30%」，使圈选/网格聚合演示有区分度。
2. 冗余列 lon/lat/grid_id **在 ETL 侧直接写入**（推荐路径，零额外开销），
   与 location 严格同源；网格公式与 fact_traffic_grid 完全一致：
       原点 (103.85, 30.45)，边长 0.01 度（约 1 km），50 列 × 40 行 = 2000 网格
       grid_id = FLOOR((lat-30.45)/0.01) * 50 + FLOOR((lon-103.85)/0.01) + 1
3. 向量：1024 维「段拼接法」构造（32 段 × 32 维），保留两级近邻结构——
   45% 段取自类目原型、30% 段取自该 POI 的语义标签原型、25% 段为噪声。
   于是 cosine 相似度的层次为：同类目同标签 > 同类目 > 异类目，语义检索演示可解释。

   ⚠️ 诚实声明：样例数据的 emb 是**确定性合成向量**（用于验证索引、算子与链路），
   并非真实语义模型输出。真实场景请替换为 BGE-m3 等模型向量，表结构与 SQL 无需改动。

4. 输出：流式写盘，不驻留内存。

用法：python backend/etl/seed_cd.py [输出目录]
"""
import json
import os
import random
import sys
import time

# ---------------- 全局参数 ----------------
SEED = 20261015
N_POI = 20_000
N_STORE = 800
N_KB = 60
DIM = 1024
SEG = 32                 # 段数
SEG_DIM = DIM // SEG     # 每段 32 维
NOISE_POOL = 500         # 每段噪声候选数
BATCH = 500              # 每条 INSERT 的行数

# 网格规格（与 fact_traffic_grid、文档 §6.3 注释保持一致）
G_ORIGIN_LON, G_ORIGIN_LAT = 103.85, 30.45
G_CELL = 0.01
G_COLS, G_ROWS = 50, 40
LON_MIN = G_ORIGIN_LON
LON_MAX = G_ORIGIN_LON + G_CELL * G_COLS      # 104.35
LAT_MIN = G_ORIGIN_LAT
LAT_MAX = G_ORIGIN_LAT + G_CELL * G_ROWS      # 30.85

# 商圈中心（lon, lat）
CBD = [
    (104.0817, 30.6570),   # 春熙路
    (104.0665, 30.6575),   # 天府广场
    (104.0632, 30.5720),   # 金融城
    (104.1155, 30.6688),   # 建设路
    (104.0186, 30.6717),   # 金沙
    (104.0400, 30.5450),   # 大源
    (103.9400, 30.7500),   # 犀浦
    (104.2700, 30.5600),   # 龙泉
]
CBD_SIGMA = 0.018

DISTRICTS = ["锦江区", "青羊区", "武侯区", "成华区", "金牛区", "高新区", "天府新区"]
ROADS = ["人民南路", "蜀汉路", "红星路", "一环路", "二环路", "天府大道", "建设路",
         "光华大道", "蓉都大道", "剑南大道", "益州大道", "成龙大道"]

PLACES = ["春熙", "太古里", "锦里", "宽窄", "玉林", "建设路", "金沙", "光华", "高新",
          "天府三街", "犀浦", "双流", "武侯", "青羊", "成华", "锦江", "金牛", "龙泉",
          "新都", "温江", "郫都", "大源", "中和", "华阳", "麓湖", "东郊", "银泰",
          "环球中心", "九眼桥", "兰桂坊", "望平街", "香香巷", "水井坊", "猛追湾",
          "电子科大", "川大", "财大", "交大", "理工", "成大"]

# ---------------- 类目与语义标签 ----------------
# (category_id, name_l1, name_l2, name_l3, 简短词, 可用标签, 名称修饰词)
CATEGORIES = [
    (1,  "餐饮", "咖啡茶饮", "咖啡馆",   "咖啡", ["quiet", "study", "wifi", "parking", "h24", "pet", "net", "biz"],
     ["", "自习", "24h", "静谧", "手冲", "精品"]),
    (2,  "餐饮", "火锅",     "川味火锅", "火锅", ["parking", "family", "net", "biz"],
     ["老", "蜀味", "串串", "牛油", "鲜切", "老灶"]),
    (3,  "餐饮", "茶饮",     "茶饮店",   "茶饮", ["wifi", "net", "cheap", "pet"],
     ["", "鲜果", "轻乳", "手作", "平价"]),
    (4,  "购物", "便利店",   "便利店",   "便利店", ["h24", "cheap", "parking"],
     ["", "24h", "社区", "枢纽"]),
    (5,  "教育", "培训机构", "自习室",   "自习室", ["quiet", "study", "wifi", "h24"],
     ["24h", "静音", "考研", "共享", "沉浸"]),
    (6,  "休闲", "电影院",   "影城",     "影城", ["parking", "family", "net"],
     ["", "IMAX", "激光", "杜比", "点播"]),
    (7,  "运动", "健身",     "健身房",   "健身", ["parking", "h24", "cheap"],
     ["", "24h", "私教", "综合", "普拉提"]),
    (8,  "生活", "美发",     "美发造型", "造型", ["net", "biz", "wifi"],
     ["", "轻奢", "日式", "快剪", "定制"]),
    (9,  "医疗", "口腔",     "口腔诊所", "口腔", ["biz", "parking", "wifi"],
     ["", "正畸", "种植", "儿牙", "综合"]),
    (10, "住宿", "经济酒店", "快捷酒店", "酒店", ["parking", "h24", "cheap", "biz"],
     ["", "商务", "轻旅", "公寓", "青旅"]),
]

TAG_CN = {"quiet": "安静", "study": "自习", "wifi": "wifi", "parking": "停车",
          "h24": "24小时", "pet": "宠物友好", "net": "网红打卡", "family": "亲子",
          "biz": "商务", "cheap": "平价"}
ALL_TAGS = list(TAG_CN.keys())

BRANDS = {1: ["瑞幸", "星巴克", "Manner", "库迪", "漫咖啡", "Seesaw"],
          2: ["小龙坎", "蜀大侠", "大龙燚", "渝味晓宇", "马路边边"],
          3: ["茶百道", "书亦", "霸王茶姬", "古茗", "沪上阿姨"],
          4: ["红旗连锁", "舞东风", "全家", "便利蜂", "美宜佳"],
          5: ["去K书", "新东方", "中公", "金榜", "启航"],
          6: ["万达影城", "太平洋", "CGV", "博纳", "UME"],
          7: ["威尔士", "乐刻", "金吉鸟", "超越", "古德菲力"],
          8: ["文峰", "永琪", "木北", "东田", "崇尚"],
          9: ["华西口腔", "拜博", "瑞泰", "美维", "牙博士"],
          10: ["如家", "汉庭", "7天", "全季", "亚朵轻居"]}

# 知识库主题（长期记忆样例）
KB_TOPICS = [
    ("咖啡馆选址要点", "咖啡馆选址优先考虑写字楼与高校周边的步行可达性，"
                    "地铁口 500 米内为佳；安静、可久坐、有电源与 wifi 是自习客群的核心诉求；"
                    "需评估同商圈 3 公里内连锁品牌密度，避免过度竞争。"),
    ("火锅店选址要点", "火锅店依赖晚间客流与聚客能力，优先选择临街一层、"
                    "具备独立排烟与停车条件的位置；社区型门店看重常住人口密度，"
                    "商圈型门店看重夜间客流与影院/商场联动。"),
    ("自习室经营模型", "付费自习室的核心指标是座位周转率与月卡复购率，"
                    "选址应贴近高校与住宅区交界，租金承受力弱于餐饮，需控制单座位面积成本。"),
    ("客流网格评估方法", "以 1 公里网格聚合常住人口与分时客流，"
                      "用工作日晚间与周末午间两个时间桶做对比，识别通勤型与休闲型商圈；"
                      "网格粒度选择需平衡样本量与空间分辨率。"),
    ("竞品密度计算", "竞品密度按网格统计同品类门店数量与加权评分，"
                  "高并发商圈需结合客流饱和度判断是红海还是虹吸效应，不能只看数量。"),
    ("租金坪效测算", "坪效 = 月营业额 / 营业面积；餐饮业态需结合翻台率与客单价反推，"
                  "租金占比建议控制在月流水的 15% 以内。"),
]


# ---------------- 向量构造 ----------------
def build_segments(rnd):
    """构建：每个语义因子的原型段 + 每段噪声候选池。返回 (proto, noise)。"""
    t0 = time.time()
    factors = ["cat%d" % c[0] for c in CATEGORIES] + ["tag_" + t for t in ALL_TAGS] \
              + ["store", "kb"]
    proto = {}
    for f in factors:
        segs = []
        for s in range(SEG):
            bias = rnd.uniform(-0.8, 0.8)
            segs.append(",".join("%.3f" % min(1.0, max(-1.0, bias + rnd.gauss(0, 0.30)))
                                 for _ in range(SEG_DIM)))
        proto[f] = segs
    # 知识库按主题细分
    for i in range(len(KB_TOPICS)):
        f = "kb%d" % i
        segs = []
        for s in range(SEG):
            bias = rnd.uniform(-0.8, 0.8)
            segs.append(",".join("%.3f" % min(1.0, max(-1.0, bias + rnd.gauss(0, 0.30)))
                                 for _ in range(SEG_DIM)))
        proto[f] = segs
    noise = []
    for s in range(SEG):
        noise.append([",".join("%.3f" % rnd.uniform(-1.0, 1.0) for _ in range(SEG_DIM))
                      for _ in range(NOISE_POOL)])
    print("[seg] 段池构建完成 %.1fs（因子 %d 个 + 噪声 %d×%d 段）"
          % (time.time() - t0, len(proto), SEG, NOISE_POOL), flush=True)
    return proto, noise


def gen_vector(rnd, proto, noise, cat_factor, tag_factors, p_noise=0.25):
    """按 45% 类目原型 / 30% 标签原型 / p_noise 噪声 拼出 1024 维向量字面量。"""
    parts = []
    for s in range(SEG):
        r = rnd.random()
        if r < 0.45:
            parts.append(proto[cat_factor][s])
        elif r < 0.75 and tag_factors:
            parts.append(proto[tag_factors[rnd.randrange(len(tag_factors))]][s])
        else:
            parts.append(noise[s][rnd.randrange(NOISE_POOL)])
    return "[" + ",".join(parts) + "]"


def gen_query_vector(proto, cat_factor, tag_factors):
    """查询向量：类目 60% + 标签 40%，不含噪声（确定性，可复现）。"""
    parts = []
    rnd = random.Random(777)
    for s in range(SEG):
        if not tag_factors or rnd.random() < 0.60:
            parts.append(proto[cat_factor][s])
        else:
            parts.append(proto[tag_factors[rnd.randrange(len(tag_factors))]][s])
    return "[" + ",".join(parts) + "]"


# ---------------- 工具 ----------------
def esc(s):
    return s.replace("\\", "\\\\").replace("'", "''")


def grid_id_of(lon, lat):
    # 必须先 round 到 6 位：写入 WKT 时是 %.6f，库内 ST_X/ST_Y 解析回来的值就是它。
    # 若用原始 double 计算，落在网格边界附近的点会与库内重算结果相差一格（实测出现过 1 行不一致）。
    lon = round(lon, 6)
    lat = round(lat, 6)
    col = int((lon - G_ORIGIN_LON) / G_CELL)
    row = int((lat - G_ORIGIN_LAT) / G_CELL)
    col = min(max(col, 0), G_COLS - 1)
    row = min(max(row, 0), G_ROWS - 1)
    return row * G_COLS + col + 1


def sample_point(rnd):
    if rnd.random() < 0.70:
        cx, cy = CBD[rnd.randrange(len(CBD))]
        lon = rnd.gauss(cx, CBD_SIGMA)
        lat = rnd.gauss(cy, CBD_SIGMA)
    else:
        lon = rnd.uniform(LON_MIN, LON_MAX)
        lat = rnd.uniform(LAT_MIN, LAT_MAX)
    lon = min(max(lon, LON_MIN + 1e-6), LON_MAX - 1e-6)
    lat = min(max(lat, LAT_MIN + 1e-6), LAT_MAX - 1e-6)
    return lon, lat


def poi_name(rnd, cat_row, tags):
    cid, _, _, _, word, _, mods = cat_row
    place = PLACES[rnd.randrange(len(PLACES))]
    # 标签驱动的名称修饰：保证全文检索「咖啡 自习」等组合能命中
    if "study" in tags and cid in (1, 5):
        return "%s%s%s" % (place, "自习", word if cid == 1 else "空间")
    if "h24" in tags:
        return "%s24h%s" % (place, word)
    if "quiet" in tags and cid == 1:
        return "%s静谧%s" % (place, word)
    mod = mods[rnd.randrange(len(mods))]
    if rnd.random() < 0.35:
        return "%s%s（%s店）" % (place, word, BRANDS[cid][rnd.randrange(len(BRANDS[cid]))])
    return "%s%s%s" % (place, mod, word)


# ---------------- 各表生成 ----------------
def gen_category(out_dir):
    p = os.path.join(out_dir, "01_seed_category.sql")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("USE geomind;\nINSERT INTO dim_category (category_id, name_l1, name_l2, name_l3) VALUES\n")
        rows = ["(%d,'%s','%s','%s')" % (c[0], esc(c[1]), esc(c[2]), esc(c[3])) for c in CATEGORIES]
        fh.write(",\n".join(rows) + ";\n")
    print("[category] %d 行" % len(CATEGORIES), flush=True)


def gen_poi(out_dir, rnd, proto, noise):
    p = os.path.join(out_dir, "02_seed_poi.sql")
    t0 = time.time()
    n = 0
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("USE geomind;\nSET autocommit=0;\n")
        buf = []
        for i in range(1, N_POI + 1):
            cat_row = CATEGORIES[rnd.randrange(len(CATEGORIES))]
            cid = cat_row[0]
            avail = cat_row[5]
            k = rnd.randrange(1, min(4, len(avail)) + 1)
            tags = rnd.sample(avail, k)
            name = poi_name(rnd, cat_row, tags)
            brand = BRANDS[cid][rnd.randrange(len(BRANDS[cid]))] if rnd.random() < 0.5 else ""
            lon, lat = sample_point(rnd)
            gid = grid_id_of(lon, lat)
            status = 1 if rnd.random() < 0.92 else 0
            rating = round(rnd.uniform(3.2, 5.0), 1)
            dist = DISTRICTS[rnd.randrange(len(DISTRICTS))]
            road = ROADS[rnd.randrange(len(ROADS))]
            address = "成都市%s%s%d号" % (dist, road, rnd.randrange(1, 999))
            open_h = "%02d:00-%02d:00" % (rnd.choice([8, 9, 10, 11]), rnd.choice([20, 21, 22, 23]))
            avg_price = {"1": 35, "2": 95, "3": 18, "4": 15}.get(str(cid), rnd.randrange(20, 200))
            attrs = {
                "open_hours": open_h,
                "parking": "parking" in tags,
                "wifi": "wifi" in tags,
                "avg_price": avg_price,
                "tags": [TAG_CN[t] for t in tags],
            }
            emb = gen_vector(rnd, proto, noise, "cat%d" % cid, ["tag_" + t for t in tags])
            buf.append(
                "(%d,'%s',%d,'%s','%s','成都',ST_GeomFromText('POINT(%.6f %.6f)'),%.6f,%.6f,%d,"
                "%.1f,'%s',%d,'%s')"
                % (i, esc(name), cid, esc(brand), esc(address), lon, lat, lon, lat, gid,
                   rating, esc(json.dumps(attrs, ensure_ascii=False)), status, emb))
            if len(buf) >= BATCH:
                fh.write("INSERT INTO dim_poi (poi_id,name,category_id,brand,address,city,"
                         "location,lon,lat,grid_id,rating,attrs,status,emb) VALUES\n"
                         + ",\n".join(buf) + ";\n")
                buf = []
                n += BATCH
                if n % 5000 == 0:
                    print("[poi] %d/%d  %.1fs" % (n, N_POI, time.time() - t0), flush=True)
        if buf:
            fh.write("INSERT INTO dim_poi (poi_id,name,category_id,brand,address,city,"
                     "location,lon,lat,grid_id,rating,attrs,status,emb) VALUES\n"
                     + ",\n".join(buf) + ";\n")
        fh.write("COMMIT;\n")
    print("[poi] 完成 %d 行, %.1f MB, %.1fs"
          % (N_POI, os.path.getsize(p) / 1e6, time.time() - t0), flush=True)


def gen_store(out_dir, rnd, proto, noise):
    p = os.path.join(out_dir, "03_seed_store.sql")
    t0 = time.time()
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("USE geomind;\nSET autocommit=0;\n")
        buf = []
        for i in range(1, N_STORE + 1):
            lon, lat = sample_point(rnd)
            gid = grid_id_of(lon, lat)
            # 租金：离最近商圈越近越贵
            dmin = min(abs(lon - c[0]) + abs(lat - c[1]) for c in CBD)
            rent = round((180 - dmin * 900) * rnd.uniform(0.6, 1.5), 2)
            rent = max(rent, 25.0)
            area = round(rnd.uniform(30, 320), 1)
            tier = rnd.choice(["W1", "W2", "W3", "W4"])
            place = PLACES[rnd.randrange(len(PLACES))]
            name = "%s%s号候选铺位" % (place, rnd.randrange(1, 999))
            attrs = {"floor": rnd.randrange(1, 4), "front_width": round(rnd.uniform(3, 15), 1),
                     "transfer_fee": rnd.randrange(0, 30) * 10000,
                     "smoke_exhaust": rnd.random() < 0.4}
            emb = gen_vector(rnd, proto, noise, "store", ["tag_parking"] if attrs["front_width"] > 8 else [])
            buf.append("(%d,'%s',%.2f,%.1f,'%s',ST_GeomFromText('POINT(%.6f %.6f)'),%.6f,%.6f,%d,'%s','%s')"
                       % (i, esc(name), rent, area, tier, lon, lat, lon, lat, gid,
                          esc(json.dumps(attrs, ensure_ascii=False)), emb))
            if len(buf) >= BATCH:
                fh.write("INSERT INTO fact_store (store_id,name,rent,area_sqm,streetfront_tier,"
                         "location,lon,lat,grid_id,attrs,emb) VALUES\n" + ",\n".join(buf) + ";\n")
                buf = []
        if buf:
            fh.write("INSERT INTO fact_store (store_id,name,rent,area_sqm,streetfront_tier,"
                     "location,lon,lat,grid_id,attrs,emb) VALUES\n" + ",\n".join(buf) + ";\n")
        fh.write("COMMIT;\n")
    print("[store] 完成 %d 行, %.1f MB, %.1fs"
          % (N_STORE, os.path.getsize(p) / 1e6, time.time() - t0), flush=True)


def gen_grid(out_dir, rnd):
    p = os.path.join(out_dir, "04_seed_grid.sql")
    t0 = time.time()
    buckets = ["workday_evening", "weekend_noon"]
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("USE geomind;\nSET autocommit=0;\n")
        buf = []
        for r in range(G_ROWS):
            for c in range(G_COLS):
                gid = r * G_COLS + c + 1
                x0 = G_ORIGIN_LON + c * G_CELL
                x1 = x0 + G_CELL
                y0 = G_ORIGIN_LAT + r * G_CELL
                y1 = y0 + G_CELL
                wkt = "POLYGON((%.6f %.6f,%.6f %.6f,%.6f %.6f,%.6f %.6f,%.6f %.6f))" % (
                    x0, y0, x1, y0, x1, y1, x0, y1, x0, y0)
                clon, clat = x0 + G_CELL / 2, y0 + G_CELL / 2
                dmin = min(abs(clon - cb[0]) + abs(clat - cb[1]) for cb in CBD)
                base_resident = int(9000 * max(0.0, 1 - dmin / 0.30) + rnd.uniform(200, 2500))
                for b in buckets:
                    k = 1.35 if b == "weekend_noon" else 1.0
                    flow = int(base_resident * rnd.uniform(0.25, 0.75) * k)
                    buf.append("(%d,'%s',ST_GeomFromText('%s'),%.6f,%.6f,%d,%d)"
                               % (gid, b, wkt, clon, clat, base_resident, flow))
                    if len(buf) >= BATCH:
                        fh.write("INSERT INTO fact_traffic_grid (grid_id,time_bucket,grid_poly,"
                                 "center_lon,center_lat,resident_cnt,flow_cnt) VALUES\n"
                                 + ",\n".join(buf) + ";\n")
                        buf = []
        if buf:
            fh.write("INSERT INTO fact_traffic_grid (grid_id,time_bucket,grid_poly,"
                     "center_lon,center_lat,resident_cnt,flow_cnt) VALUES\n" + ",\n".join(buf) + ";\n")
        fh.write("COMMIT;\n")
    print("[grid] 完成 %d 行（%d 网格 × %d 时段）, %.1f MB, %.1fs"
          % (G_COLS * G_ROWS * len(buckets), G_COLS * G_ROWS, len(buckets),
             os.path.getsize(p) / 1e6, time.time() - t0), flush=True)


def gen_kb(out_dir, rnd, proto, noise):
    p = os.path.join(out_dir, "05_seed_kb.sql")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("USE geomind;\nSET autocommit=0;\n")
        buf = []
        for i in range(N_KB):
            topic_idx = i % len(KB_TOPICS)
            title, base_text = KB_TOPICS[topic_idx]
            title = "%s（%d）" % (title, i // len(KB_TOPICS) + 1)
            text = base_text + "本条为第 %d 号切片，用于验证全文与向量的混合召回。" % (i + 1)
            emb = gen_vector(rnd, proto, noise, "kb%d" % topic_idx,
                             ["tag_biz"] if topic_idx % 2 == 0 else ["tag_cheap"])
            meta = {"topic": KB_TOPICS[topic_idx][0], "source": "内部方法论", "version": 1}
            buf.append("(%d,'%s','%s','%s','%s')"
                       % (i + 1, esc(title), esc(text), esc(json.dumps(meta, ensure_ascii=False)), emb))
            if len(buf) >= BATCH:
                fh.write("INSERT INTO knowledge_doc (doc_id,title,text,meta,emb) VALUES\n"
                         + ",\n".join(buf) + ";\n")
                buf = []
        if buf:
            fh.write("INSERT INTO knowledge_doc (doc_id,title,text,meta,emb) VALUES\n"
                     + ",\n".join(buf) + ";\n")
        fh.write("COMMIT;\n")
    print("[kb] 完成 %d 行" % N_KB, flush=True)


def gen_scene(out_dir):
    p = os.path.join(out_dir, "06_seed_scene.sql")
    state = {
        "session_id": "demo-site-selection-001",
        "scene": "site_selection",
        "version": 1,
        "slots": {
            "intent": "为咖啡馆选址",
            "city": "成都",
            "category": "咖啡茶饮",
            "center": {"lon": 104.0817, "lat": 30.6570, "name": "春熙路"},
            "radius_m": 1500,
            "budget_rent": 40000,
            "must_tags": ["安静", "自习"]
        },
        "steps": [
            {"id": "s1_candidates", "skill": "hybrid_search", "status": "done",
             "result_ref": "candidates", "derived_from": ["dim_poi"]},
            {"id": "s2_spatial_join", "skill": "spatial_join_agg", "status": "ready",
             "depends_on": ["s1_candidates"],
             "derived_from": ["candidates", "fact_traffic_grid"]},
            {"id": "s3_score", "skill": "score_and_rank", "status": "pending",
             "depends_on": ["s2_spatial_join"]}
        ],
        "layers": [
            {"id": "l_candidates", "kind": "points", "data_ref": "candidates", "status": "ready",
             "derived_from": ["s1_candidates"]},
            {"id": "l_heat", "kind": "heatmap", "data_ref": "fact_traffic_grid",
             "style": {"radius": 24}, "status": "ready", "derived_from": ["s2_spatial_join"]}
        ],
        "evidence": [{"type": "sql", "ref": "hybrid_search", "note": "四模混合检索"}],
        "updated_at": "2026-10-15T15:30:00"
    }
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("USE geomind;\n")
        fh.write("INSERT INTO scene_solution (session_id,scene,version,state,created_at,updated_at) VALUES\n")
        fh.write("('demo-site-selection-001','site_selection',1,'%s',NOW(),NOW())\n"
                 % esc(json.dumps(state, ensure_ascii=False)))
        fh.write("ON DUPLICATE KEY UPDATE version=version+1, state=VALUES(state);\n")
    print("[scene] 1 行（Spatial State 样例）", flush=True)


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(out_dir, exist_ok=True)
    rnd = random.Random(SEED)
    proto, noise = build_segments(rnd)

    gen_category(out_dir)
    gen_poi(out_dir, rnd, proto, noise)
    gen_store(out_dir, rnd, proto, noise)
    gen_grid(out_dir, rnd)
    gen_kb(out_dir, rnd, proto, noise)
    gen_scene(out_dir)

    # 查询向量（供验证脚本注入，与数据同源）
    q1 = gen_query_vector(proto, "cat1", ["tag_quiet", "tag_study"])
    with open(os.path.join(out_dir, "qvec_coffee_quiet.txt"), "w", encoding="utf-8") as fh:
        fh.write(q1)
    q2 = gen_query_vector(proto, "kb0", ["tag_biz"])
    with open(os.path.join(out_dir, "qvec_kb.txt"), "w", encoding="utf-8") as fh:
        fh.write(q2)
    print("[qvec] 咖啡+安静/自习 查询向量 %d 维；知识库查询向量 %d 维"
          % (q1.count(",") + 1, q2.count(",") + 1), flush=True)
    print("[done] 输出目录: %s" % out_dir, flush=True)


if __name__ == "__main__":
    main()
