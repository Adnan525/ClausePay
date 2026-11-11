import ast
import json
import subprocess
import sys
from decimal import Decimal
from typing import Any

from clausepay.fees import payout_fee_settings

MAX_CODE_CHARS = 20_000
TIMEOUT_SECONDS = 5

# The two functions a contract must define, with their exact parameter names.
REQUIRED_FUNCTIONS: dict[str, list[str]] = {
    "calculate_payout": ["sale_id", "sale_amount", "paypal_fee", "state"],
    "calculate_refund": ["sale_id", "refund_amount", "fee_returned", "state"],
}

BANNED_NODES: dict[type, str] = {
    ast.Import: "imports are not allowed",
    ast.ImportFrom: "imports are not allowed",
    ast.Global: "global is not allowed",
    ast.Nonlocal: "nonlocal is not allowed",
    ast.While: "while loops are not allowed (use for loops)",
    ast.AsyncFunctionDef: "async code is not allowed",
    ast.Await: "async code is not allowed",
    ast.Yield: "generators are not allowed",
    ast.YieldFrom: "generators are not allowed",
    ast.ClassDef: "classes are not allowed",
    ast.With: "with blocks are not allowed",
    ast.AsyncWith: "async code is not allowed",
    ast.AsyncFor: "async code is not allowed",
    ast.Pow: "the ** operator is not allowed",
}

BANNED_NAMES = {
    "eval", "exec", "open", "compile", "getattr", "setattr", "delattr", "globals", "locals", "vars", "dir",
    "input", "print", "type", "object", "breakpoint", "float", "complex", "bytes", "bytearray", "memoryview",
    "super", "property", "classmethod", "staticmethod", "help", "exit", "quit",
}

# Names the contract code may use besides its own. Anything else does not exist at runtime.
ALLOWED_BUILTINS = [
    "min", "max", "sum", "abs", "len", "range", "sorted", "dict", "list", "tuple", "str", "int", "bool",
    "enumerate", "zip", "any", "all", "reversed", "round", "ValueError", "True", "False", "None",
]


class SandboxError(Exception):
    """The contract code failed to run (error, timeout, or unusable output)."""


def check_code(code: str) -> list[str]:
    """Reasons this code is not acceptable; empty means it passed the static check."""
    if len(code) > MAX_CODE_CHARS:
        return [f"code is longer than {MAX_CODE_CHARS} characters"]
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        return [f"syntax error on line {error.lineno}: {error.msg}"]
    problems: list[str] = []
    for node in ast.walk(tree):
        for banned, reason in BANNED_NODES.items():
            if isinstance(node, banned):
                problems.append(f"line {getattr(node, 'lineno', '?')}: {reason}")
        if isinstance(node, ast.Name) and (node.id.startswith("__") or node.id in BANNED_NAMES):
            problems.append(f"line {node.lineno}: the name '{node.id}' is not allowed")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            problems.append(f"line {node.lineno}: private attributes ('{node.attr}') are not allowed")
        if isinstance(node, ast.Constant) and isinstance(node.value, (float, complex)):
            problems.append(f"line {node.lineno}: floating-point numbers are not allowed (use Decimal)")
    return problems + _check_signatures(tree)


def _check_signatures(tree: ast.Module) -> list[str]:
    defined = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    problems: list[str] = []
    for name, expected in REQUIRED_FUNCTIONS.items():
        function = defined.get(name)
        if function is None:
            problems.append(f"missing function {name}")
            continue
        actual = [argument.arg for argument in function.args.args]
        if actual != expected or function.args.vararg or function.args.kwarg or function.args.kwonlyargs:
            problems.append(f"{name} must have exactly the parameters ({', '.join(expected)}), got ({', '.join(actual)})")
    return problems


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"__decimal__": str(value)}
    raise TypeError(f"cannot send {type(value).__name__} out of the sandbox")


def _decode(obj: dict[str, Any]) -> Any:
    if len(obj) == 1 and "__decimal__" in obj:
        return Decimal(obj["__decimal__"])
    return obj


def to_json(value: Any) -> str:
    """JSON text for `value` in which Decimals stay exact (used for state and journal files)."""
    return json.dumps(value, default=_encode, indent=2)


def from_json(text: str) -> Any:
    """The inverse of to_json."""
    return json.loads(text, object_hook=_decode)


_RUNNER = r'''
import builtins, json, sys
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
try:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
except Exception:
    pass


def encode(value):
    if isinstance(value, Decimal):
        return {"__decimal__": str(value)}
    raise TypeError("cannot return " + type(value).__name__)


def decode(obj):
    if len(obj) == 1 and "__decimal__" in obj:
        return Decimal(obj["__decimal__"])
    return obj


payload = json.loads(sys.stdin.read(), object_hook=decode)
safe = {name: getattr(builtins, name) for name in payload["allowed"]}
RATE = Decimal(payload["payout_fee_rate"])
CAP = Decimal(payload["payout_fee_cap"]) if payload.get("payout_fee_cap") else None


def payout_fee(amount):
    fee = (Decimal(amount) * RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return min(fee, CAP) if CAP is not None else fee


scope = {"__builtins__": safe, "Decimal": Decimal, "ROUND_HALF_UP": ROUND_HALF_UP,
         "ROUND_DOWN": ROUND_DOWN, "CENT": Decimal("0.01"), "payout_fee": payout_fee}
try:
    exec(compile(payload["code"], "<contract>", "exec"), scope)
    result = scope[payload["function"]](**payload["kwargs"])
    print(json.dumps({"ok": result}, default=encode))
except BaseException as error:
    print(json.dumps({"error": type(error).__name__ + ": " + str(error)[:300]}))
'''


def run_function(code: str, function: str, kwargs: dict[str, Any]) -> Any:
    """Run one contract function in a locked-down subprocess and return its result."""
    problems = check_code(code)
    if problems:
        raise SandboxError("; ".join(problems))
    rate, cap = payout_fee_settings()
    payload = json.dumps(
        {"code": code, "function": function, "kwargs": kwargs, "allowed": ALLOWED_BUILTINS,
         "payout_fee_rate": str(rate), "payout_fee_cap": None if cap is None else str(cap)}, default=_encode
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", _RUNNER],
            input=payload, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        raise SandboxError(f"{function} took longer than {TIMEOUT_SECONDS} seconds") from None
    try:
        reply = json.loads(completed.stdout.strip().splitlines()[-1], object_hook=_decode)
    except (IndexError, ValueError):
        raise SandboxError(f"{function} crashed or returned nothing") from None
    if "error" in reply:
        raise SandboxError(f"{function} raised {reply['error']}")
    return reply["ok"]
