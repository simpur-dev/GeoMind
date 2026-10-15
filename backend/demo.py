"""CLI 演示入口：一条命令跑通 7 步闭环。

    python -m backend.demo "春熙路附近安静可自习的咖啡馆"
    python -m backend.demo "找个咖啡馆"            # 触发追问，进入交互
    python -m backend.demo --skills                # 列出已注册 Skill
"""
from __future__ import annotations

import argparse
import json

from backend.app import build_services, build_skill_context
from backend.brain.graph import resume_query, run_query
from backend.skills.registry import SkillRegistry


def _print_result(r: dict) -> None:
    if r.get("interrupted"):
        q = r.get("question") or {}
        print("\n[追问]", q.get("question", "请补充信息"))
        print("      （直接输入补充内容后回车；输入 skip 用默认值继续）")
        return
    st = r.get("state", {})
    intent = st.get("intent", {})
    print(f"\n场景: {intent.get('scene')}  置信度: {intent.get('confidence')}  "
          f"命中: {', '.join(intent.get('hits', []))}")
    cands = st.get("candidates", [])
    print(f"\n候选 POI（{len(cands)} 个，四模混合检索）:")
    for c in cands[:10]:
        print(f"  {c['poi_id']:>6}  {c['name']:<14} {c.get('dist_m'):>6} m  "
              f"语义距离 {c.get('sem_dist')}")
    aggs = st.get("aggs", [])
    print(f"\n网格聚合（{len(aggs)} 个，按客流降序 top5）:")
    for a in aggs[:5]:
        print(f"  grid {a['grid_id']:>5}  flow={a.get('flow')}  poi={a.get('poi_cnt')}  "
              f"评分={a.get('avg_rating')}")
    print("\n决策综合:")
    print("  " + (st.get("decision", {}).get("summary", "") or "")[:300])
    spec = st.get("map_spec", {})
    layers = spec.get("layers", [])
    print(f"\nMap Spec: {len(layers)} 个图层 ({', '.join(x['kind'] for x in layers)})")
    print("\nMap Spec JSON:")
    print(json.dumps(spec, ensure_ascii=False, indent=2)[:1200])


def main() -> None:
    ap = argparse.ArgumentParser(description="GeoMind CLI 演示")
    ap.add_argument("query", nargs="*", help="自然语言查询")
    ap.add_argument("--skills", action="store_true", help="列出已注册 Skill")
    ap.add_argument("--session", default="cli-demo", help="会话 id")
    args = ap.parse_args()

    services = build_services()

    if args.skills:
        ctx = build_skill_context(args.session, services)
        for d in SkillRegistry.instance().describe_all():
            print(f"{d['name']:<22} [{d['scene']:<14}] {d['description']}")
        return

    query = " ".join(args.query).strip()
    if not query:
        ap.error("请给出查询，或用 --skills 列出能力")

    ctx = build_skill_context(args.session, services)
    r = run_query(query, ctx, thread_id=args.session)
    _print_result(r)

    while r.get("interrupted"):
        try:
            answer = input("\n补充 > ").strip()
        except EOFError:
            break
        if answer.lower() in ("skip", "跳过", ""):
            answer = "成都"
        r = resume_query(answer, ctx, thread_id=args.session)
        _print_result(r)


if __name__ == "__main__":
    main()
