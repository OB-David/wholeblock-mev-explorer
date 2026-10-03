import argparse
from pathlib import Path

from analyzer import analyzer_from_env


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取整块 trace 并生成 token-flow graph JSON")
    parser.add_argument("block", nargs="?", default="latest", help="区块号或 latest")
    parser.add_argument("--force", action="store_true", help="忽略已有 graph.json 并重新分析")
    args = parser.parse_args()
    analyzer = analyzer_from_env(Path(__file__).resolve().parent.parent / "data")

    def progress(done: int, total: int, message: str) -> None:
        print(f"[{done}/{total}] {message}", flush=True)

    graph = analyzer.analyze(args.block, progress, force=args.force)
    print(f"block={graph['block']['number']} tx={len(graph['transactions'])} edges={len(graph['edges'])}")


if __name__ == "__main__":
    main()
