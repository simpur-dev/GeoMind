# -*- coding: utf-8 -*-
"""
GeoMind 10 万级 POI 压测数据生成器

设计要点：
1. 坐标：成都主城范围 WGS84 经纬度，DDL 一律 SRID 0（SeekDB 不支持 SRID 4326）
2. 向量：1024 维，采用「段拼接法」生成——预生成段池，行向量由 32 个段拼成，
   其中 70% 段取自所属语义簇原型、30% 为随机段。既保证生成速度（比逐维格式化快 ~10x），
   又保留真实的近邻结构（同簇行相似度高），使 HNSW 召回测试有意义。
3. 输出：流式写盘，不驻留内存（主机可用内存有限）。

用法：python gen_poi.py [输出目录]
"""
import os
import random
import sys
import time

# ---------------- 参数 ----------------
SEED = 20261015
N_NOVEC = 80_000        # 无向量行（id 1 ~ 80000）
N_VEC = 20_000          # 带向量行（id 80001 ~ 100000）
N_TOTAL = N_NOVEC + N_VEC
DIM = 1024
SEG = 32                # 段数
SEG_DIM = DIM // SEG    # 每段 32 维
N_CLUSTER = 50          # 语义簇数
POOL_PER_SEG = 500      # 每段的随机候选数
BATCH = 500             # 每条 INSERT 的行数

# 成都主城范围
LON_MIN, LON_MAX = 103.85, 104.35
LAT_MIN, LAT_MAX = 30.45, 30.85

CITY_CODE = "510100"

# 20 个 POI 类别
CATEGORIES = [
    "咖啡馆", "火锅店", "书店", "便利店", "中餐厅", "快餐店", "面包烘焙", "茶饮店",
    "健身房", "药店", "花店", "宠物店", "理发店", "洗车场", "停车场", "充电站",
    "幼儿园", "诊所", "银行网点", "快递驿站",
]

PREFIX = ["锦里", "春熙", "高新", "天府", "武侯", "青羊", "成华", "金牛", "龙泉",
          "新都", "温江", "郫都", "双流", "金融城", "太古里", "宽窄", "玉林",
          "建设路", "交子", "蓉城"]
MID = ["东路", "西路", "南路", "北路", "中路", "大道", "巷", "街", "广场", "公园"]
BRAND = ["星野", "蜀味", "锦书", "邻里", "老灶", "快客", "麦语", "青芽",
         "劲能", "康年", "花间", "萌宠", "型式", "净驰", "安泊", "电擎",
         "童蒙", "济世", "汇通", "速达"]


def build_segments(rnd):
    """预生成：每簇每段的原型段 + 每段的随机候选池。"""
    t0 = time.time()
    proto = []           # proto[c][s] -> str(32 个 float 拼接)
    for c in range(N_CLUSTER):
        segs = []
        for s in range(SEG):
            # 每簇给一个簇中心偏置，使同簇段彼此接近
            bias = rnd.uniform(-1.0, 1.0)
            segs.append(",".join("%.3f" % min(1.0, max(-1.0, bias + rnd.gauss(0, 0.25)))
                                 for _ in range(SEG_DIM)))
        proto.append(segs)
    pool = []            # pool[s][k] -> str
    for s in range(SEG):
        pool.append([",".join("%.3f" % rnd.uniform(-1.0, 1.0) for _ in range(SEG_DIM))
                     for _ in range(POOL_PER_SEG)])
    print("[seg] 段池构建完成 %.1fs" % (time.time() - t0), flush=True)
    return proto, pool


def gen_vector(rnd, proto, pool, cluster):
    """70% 段取自簇原型，30% 取随机段；拼成 1024 维向量字面量。"""
    parts = []
    for s in range(SEG):
        if rnd.random() < 0.7:
            parts.append(proto[cluster][s])
        else:
            parts.append(pool[s][rnd.randrange(POOL_PER_SEG)])
    return "[" + ",".join(parts) + "]"


def esc(s):
    return s.replace("\\", "\\\\").replace("'", "''")


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(out_dir, exist_ok=True)
    rnd = random.Random(SEED)
    proto, pool = build_segments(rnd)

    # ---------- 文件 1：无向量行 ----------
    f1 = os.path.join(out_dir, "02_poi_novec.sql")
    t0 = time.time()
    n = 0
    with open(f1, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("USE geomind_bench;\nSET autocommit=0;\n")
        buf = []
        for i in range(1, N_NOVEC + 1):
            cat = i % len(CATEGORIES)
            prefix = PREFIX[i % len(PREFIX)]
            mid = MID[(i // len(PREFIX)) % len(MID)]
            name = "%s%s%d号%s" % (prefix, mid, i % 997, CATEGORIES[cat])
            brand = BRAND[cat]
            status = 1 if rnd.random() < 0.9 else 0
            rating = "%.1f" % rnd.uniform(3.0, 5.0)
            lon = "%.6f" % rnd.uniform(LON_MIN, LON_MAX)
            lat = "%.6f" % rnd.uniform(LAT_MIN, LAT_MAX)
            price = rnd.randrange(15, 300)
            level = rnd.randrange(1, 6)
            props = '{"brand":"%s","price":%d,"level":%d,"tags":["wifi","parking"]}' % (esc(brand), price, level)
            buf.append("(%d,'%s',%d,%d,%s,'%s','%s',ST_GeomFromText('POINT(%s %s)'),NULL)"
                       % (i, esc(name), cat, status, rating, CITY_CODE, props, lon, lat))
            if len(buf) >= BATCH:
                fh.write("INSERT INTO t_bench_poi VALUES\n" + ",\n".join(buf) + ";\n")
                buf = []
                n += BATCH
                if n % 20000 == 0:
                    print("[novec] %d/%d  %.1fs" % (n, N_NOVEC, time.time() - t0), flush=True)
        if buf:
            fh.write("INSERT INTO t_bench_poi VALUES\n" + ",\n".join(buf) + ";\n")
        fh.write("COMMIT;\n")
    sz1 = os.path.getsize(f1)
    print("[novec] 完成 %d 行, %.1f MB, %.1fs" % (N_NOVEC, sz1 / 1e6, time.time() - t0), flush=True)

    # ---------- 文件 2：带向量行 ----------
    f2 = os.path.join(out_dir, "03_poi_vec.sql")
    t0 = time.time()
    n = 0
    with open(f2, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("USE geomind_bench;\nSET autocommit=0;\n")
        buf = []
        for k in range(N_VEC):
            i = N_NOVEC + k + 1
            cluster = i % N_CLUSTER
            cat = i % len(CATEGORIES)
            prefix = PREFIX[i % len(PREFIX)]
            mid = MID[(i // len(PREFIX)) % len(MID)]
            name = "%s%s%d号%s" % (prefix, mid, i % 997, CATEGORIES[cat])
            brand = BRAND[cat]
            status = 1 if rnd.random() < 0.9 else 0
            rating = "%.1f" % rnd.uniform(3.0, 5.0)
            lon = "%.6f" % rnd.uniform(LON_MIN, LON_MAX)
            lat = "%.6f" % rnd.uniform(LAT_MIN, LAT_MAX)
            price = rnd.randrange(15, 300)
            level = rnd.randrange(1, 6)
            props = '{"brand":"%s","price":%d,"level":%d,"tags":["wifi","parking"]}' % (esc(brand), price, level)
            emb = gen_vector(rnd, proto, pool, cluster)
            buf.append("(%d,'%s',%d,%d,%s,'%s','%s',ST_GeomFromText('POINT(%s %s)'),'%s')"
                       % (i, esc(name), cat, status, rating, CITY_CODE, props, lon, lat, emb))
            if len(buf) >= BATCH:
                fh.write("INSERT INTO t_bench_poi VALUES\n" + ",\n".join(buf) + ";\n")
                buf = []
                n += BATCH
                if n % 5000 == 0:
                    print("[vec] %d/%d  %.1fs" % (n, N_VEC, time.time() - t0), flush=True)
        if buf:
            fh.write("INSERT INTO t_bench_poi VALUES\n" + ",\n".join(buf) + ";\n")
        fh.write("COMMIT;\n")
    sz2 = os.path.getsize(f2)
    print("[vec] 完成 %d 行, %.1f MB, %.1fs" % (N_VEC, sz2 / 1e6, time.time() - t0), flush=True)

    # ---------- 文件 3：网格表（空间连接压测用） ----------
    f3 = os.path.join(out_dir, "04_grid.sql")
    ncol, nrow = 10, 10
    dlon = (LON_MAX - LON_MIN) / ncol
    dlat = (LAT_MAX - LAT_MIN) / nrow
    with open(f3, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("USE geomind_bench;\nINSERT INTO t_bench_grid (grid_id, cell) VALUES\n")
        rows = []
        gid = 0
        for r in range(nrow):
            for c in range(ncol):
                gid += 1
                x0 = LON_MIN + c * dlon
                x1 = x0 + dlon
                y0 = LAT_MIN + r * dlat
                y1 = y0 + dlat
                wkt = "POLYGON((%.6f %.6f,%.6f %.6f,%.6f %.6f,%.6f %.6f,%.6f %.6f))" % (
                    x0, y0, x1, y0, x1, y1, x0, y1, x0, y0)
                rows.append("(%d,ST_GeomFromText('%s'))" % (gid, wkt))
        fh.write(",\n".join(rows) + ";\n")
    print("[grid] %d 个网格 -> 04_grid.sql" % gid, flush=True)
    print("[done] 输出目录: %s  合计 %.1f MB, %d 行"
          % (out_dir, (sz1 + sz2) / 1e6, N_TOTAL), flush=True)


if __name__ == "__main__":
    main()
