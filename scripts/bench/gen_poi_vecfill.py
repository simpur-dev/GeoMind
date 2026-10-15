# -*- coding: utf-8 -*-
"""
把 t_bench_poi 的向量从 2 万行补齐到 10 万行（给 id 1~80000 回填 emb）。

关键点：字段生成顺序与 gen_poi.py 的无向量循环**完全一致**（status → rating → lon → lat
→ price → level），只在最后追加一次 gen_vector 调用。因此 rnd 状态在追加点之前不漂移，
回填行的其它字段与原行保持一致，只有 emb 被新生成。

输出：INSERT ... ON DUPLICATE KEY UPDATE emb=VALUES(emb)，批量 500 行/条。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import random

from gen_poi import (
    BRAND,
    CATEGORIES,
    CITY_CODE,
    LAT_MAX,
    LAT_MIN,
    LON_MAX,
    LON_MIN,
    MID,
    N_CLUSTER,
    PREFIX,
    SEED,
    build_segments,
    esc,
    gen_vector,
)

N_FILL = 80_000
BATCH = 500


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(out_dir, exist_ok=True)
    rnd = random.Random(SEED)
    proto, pool = build_segments(rnd)

    out = os.path.join(out_dir, "08_poi_vec_fill.sql")
    t0 = time.time()
    n = 0
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("USE geomind_bench;\nSET autocommit=0;\n")
        buf = []
        for i in range(1, N_FILL + 1):
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
            # ↓↓↓ 追加点：只在这里新增向量生成，保证前面字段与原始数据一致
            emb = gen_vector(rnd, proto, pool, i % N_CLUSTER)
            props = '{"brand":"%s","price":%d,"level":%d,"tags":["wifi","parking"]}' % (esc(brand), price, level)
            buf.append("(%d,'%s',%d,%d,%s,'%s','%s',ST_GeomFromText('POINT(%s %s)'),'%s')"
                       % (i, esc(name), cat, status, rating, CITY_CODE, props, lon, lat, emb))
            if len(buf) >= BATCH:
                # 显式列名：表上后续 ALTER 追加了 lon/lat/grid_id 等列
                fh.write("INSERT INTO t_bench_poi (id,name,category,status,rating,"
                         "city_code,props,location,emb) VALUES\n" + ",\n".join(buf) +
                         "\nON DUPLICATE KEY UPDATE emb=VALUES(emb);\n")
                buf = []
                n += BATCH
                if n % 20000 == 0:
                    print("[fill] %d/%d  %.1fs" % (n, N_FILL, time.time() - t0), flush=True)
        if buf:
            fh.write("INSERT INTO t_bench_poi (id,name,category,status,rating,"
                     "city_code,props,location,emb) VALUES\n" + ",\n".join(buf) +
                     "\nON DUPLICATE KEY UPDATE emb=VALUES(emb);\n")
        fh.write("COMMIT;\n")
    print("[fill] 完成 %d 行, %.1f MB, %.1fs -> %s"
          % (N_FILL, os.path.getsize(out) / 1e6, time.time() - t0, out), flush=True)


if __name__ == "__main__":
    main()
