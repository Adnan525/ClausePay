"""Local web app: submit a contract, answer clarifying questions, get the refined contract."""
import anthropic
from flask import Flask, jsonify, request

from clausepay.contract_analyzer import AnalyzerError, ContractAnalyzer
from clausepay.contract_reader import ContractReadError, read_contract
from clausepay.contract_store import ContractStore, DeedNotFound, JsonContractStore
from clausepay.deed import Deed, validate_payees


def create_app(store: ContractStore = None, analyzer: ContractAnalyzer = None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
    store = store or JsonContractStore()
    analyzer = analyzer or ContractAnalyzer()

    def view(deed):
        return {
            "deed_id": deed.deed_id,
            "status": deed.status,
            "contract_text": deed.contract_text,
            "questions": deed.pending_questions,
            "refined_contract": deed.refined_contract,
            "payees": deed.payees,
        }

    def error(message, code):
        return jsonify(error=message), code

    def apply_analysis(deed):
        """Runs the analyzer and records the outcome on the deed (not yet saved)."""
        result = analyzer.analyze(deed.contract_text, deed.clarifications)
        deed.pending_questions = result["questions"]
        deed.refined_contract = result["refined_contract"]
        deed.status = "clarifying" if result["status"] == "needs_answers" else "refined"

    @app.errorhandler(DeedNotFound)
    def not_found(e):
        return error("deed not found", 404)

    @app.errorhandler(413)
    def too_large(e):
        return error("file is too large (limit 5 MB)", 413)

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.post("/api/deeds")
    def create_deed():
        text = (request.form.get("contract_text") or "").strip()
        upload = request.files.get("file")
        try:
            if upload and upload.filename:
                text = read_contract(upload.filename, upload.read())
        except ContractReadError as e:
            return error(str(e), 400)
        if not text:
            return error("paste a contract or attach a file", 400)

        deed = Deed(contract_text=text)
        try:
            apply_analysis(deed)
        except (AnalyzerError, anthropic.APIError) as e:
            return error(f"could not analyse the contract: {e}", 502)
        store.add(deed)
        return jsonify(view(deed)), 201

    @app.post("/api/deeds/<deed_id>/answers")
    def answer(deed_id):
        deed = store.get(deed_id)
        if deed.status != "clarifying":
            return error("this contract has no open questions", 409)
        answers = (request.get_json(silent=True) or {}).get("answers")
        asked = {q["question"] for q in deed.pending_questions}
        if not answers or any(not a.get("answer", "").strip() or a.get("question") not in asked for a in answers):
            return error("answer every question", 400)
        if {a["question"] for a in answers} != asked:
            return error("answer every question", 400)

        deed.clarifications += [{"question": a["question"], "answer": a["answer"].strip()} for a in answers]
        try:
            apply_analysis(deed)
        except (AnalyzerError, anthropic.APIError) as e:
            return error(f"could not analyse the answers: {e}", 502)
        store.save(deed)
        return jsonify(view(deed))

    @app.post("/api/deeds/<deed_id>/payees")
    def set_payees(deed_id):
        deed = store.get(deed_id)
        if deed.status not in ("refined", "payees_collected"):
            return error("the contract is not ready for payees yet", 409)
        submitted = (request.get_json(silent=True) or {}).get("payees")
        expected = sorted(p["name"] for p in deed.refined_contract["payees"])
        if not isinstance(submitted, list) or sorted(str(p.get("name")) for p in submitted) != expected:
            return error("give an email for every payee listed in the contract", 400)
        payees = [{"name": p["name"], "email": str(p.get("email", "")).strip()} for p in submitted]
        try:
            validate_payees(payees)
        except ValueError:
            return error("every payee needs a valid email address", 400)

        deed.payees = payees
        deed.status = "payees_collected"
        store.save(deed)
        return jsonify(view(deed))

    @app.get("/api/deeds/<deed_id>")
    def get_deed(deed_id):
        return jsonify(view(store.get(deed_id)))

    return app
