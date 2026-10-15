# -*- coding: utf-8 -*-
"""
把压测模板中的 {QVEC} 占位符替换为真实的 1024 维查询向量。

查询向量取自生成数据本身（03_poi_vec.sql 中第一行的 emb），保证它落在真实数据分布内，
使 ANN 召回测试有意义。流式读取，避免把 130 MB 文件整体载入内存。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SQL = os.path.join(HERE, "sql")

VEC_SRC = os.path.join(DATA, "03_poi_vec.sql")
PAIRS = [
    (os.path.join(SQL, "05_bench_tpl.sql"), os.path.join(DATA, "05_bench.sql")),
    (os.path.join(SQL, "06_bench_vec_tpl.sql"), os.path.join(DATA, "06_bench_vec.sql")),
    (os.path.join(SQL, "07_timing_tpl.sql"), os.path.join(DATA, "07_timing.sql")),
    (os.path.join(SQL, "09_vec10w_tpl.sql"), os.path.join(DATA, "09_vec10w.sql")),
]


MIN_DIM = 512  # 跳过 JSON 里的 ["wifi","parking"] 之类的短数组


def extract_query_vector(path):
    """流式扫描，取第一个维度 >= MIN_DIM 的 [ ... ] 向量字面量。"""
    buf = []
    in_vec = False
    with open(path, "r", encoding="utf-8") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            for ch in chunk:
                if not in_vec:
                    if ch == "[":
                        in_vec = True
                        buf = ["["]
                    continue
                buf.append(ch)
                if ch == "]":
                    v = "".join(buf)
                    dim = v.count(",") + 1
                    if dim >= MIN_DIM:
                        print("[qvec] 提取到 %d 维向量，长度 %d 字符" % (dim, len(v)), flush=True)
                        return v
                    # 维度不够（如 JSON 短数组），放弃并继续扫描
                    in_vec = False
                    buf = []
    raise SystemExit("未能在 %s 中找到 >= %d 维的向量字面量" % (path, MIN_DIM))


def main():
    qvec = extract_query_vector(VEC_SRC)
    for tpl, out in PAIRS:
        with open(tpl, "r", encoding="utf-8") as fh:
            content = fh.read()
        content = content.replace("{QVEC}", qvec)
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        print("[build] %s -> %s (%.1f KB)" % (os.path.basename(tpl), os.path.basename(out),
                                              os.path.getsize(out) / 1024), flush=True)


if __name__ == "__main__":
    main()
