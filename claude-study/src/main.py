import argparse
import sys

from src.calc import add, subtract, multiply, divide

OPS = {
    "add": add,
    "subtract": subtract,
    "multiply": multiply,
    "divide": divide,
}


def main():
    parser = argparse.ArgumentParser(description="簡単なCLI電卓")
    parser.add_argument("a", type=float, help="左辺の数値")
    parser.add_argument("op", choices=OPS.keys(), help="演算子 (add/subtract/multiply/divide)")
    parser.add_argument("b", type=float, help="右辺の数値")
    args = parser.parse_args()

    try:
        result = OPS[args.op](args.a, args.b)
        print(result)
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
