"""Start the ClausePay demo web server: `python main.py` (then open http://127.0.0.1:8000)."""
import os

from clausepay.webapp import create_app


def main() -> None:
    port = int(os.getenv("PORT", "8000"))
    create_app().run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
