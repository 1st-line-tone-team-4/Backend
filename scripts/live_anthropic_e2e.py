"""Explicitly authorized synthetic live tests, with one persistent budget ledger.

Never run from CI. The real key is read only from the operator-designated file,
held in memory, and supplied only to api.anthropic.com. A memory-only credential
adapter replaces the normal encrypted vault for this narrowly scoped test.
"""

import argparse
from decimal import Decimal, InvalidOperation
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import select

from app.core import ApiError, Settings, uid
from app.models import Job
from app.providers import ProviderFailure, ProviderRegistry, strict_json
from scripts.credential_input import CredentialInputError, MAX_INPUT_BYTES, parse_credential_bytes
from tests.support import test_application as make_test_application

MODEL = "anthropic:claude-haiku-5-5"
OUTPUT_CAP = 4096
# Full 1M context at the more expensive >100k tier, plus all output/reasoning.
# No server tools, caches, batches, speed premiums or retries are enabled.
RESERVATION = (Decimal(1_000_000) * Decimal("0.50") + Decimal(OUTPUT_CAP) * Decimal("2.50")) / Decimal(
    1_000_000
)
MAX_BUDGET = Decimal("10")
MAX_CALLS = 8
SOURCE = "Momentum is conserved when the total external force is zero. Momentum equals mass times velocity. In an isolated collision, total momentum before and after the collision is equal."


class Ledger:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.path.with_suffix(self.path.suffix + ".lock")
        self.lock_fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        self.lock_owned = True
        self.mutex = threading.Lock()
        self.active = None
        self.write_failed = False
        try:
            self.data = self._load()
            self._validate()
        except BaseException:
            self.close()
            raise

    def _load(self):
        if not self.path.exists():
            return {"model": MODEL, "budget_usd": "10", "entries": [], "runs": []}
        try:
            return strict_json(self.path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            pass
        raise ValueError("LEDGER_INVALID")

    def _validate(self):
        if (
            not isinstance(self.data, dict)
            or self.data.get("model") != MODEL
            or self.data.get("budget_usd") != "10"
            or not isinstance(self.data.get("entries"), list)
            or not isinstance(self.data.get("runs"), list)
        ):
            raise ValueError("LEDGER_INVALID")
        entries = self.data["entries"]
        reserved = Decimal(0)
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or entry.get("number") != index + 1:
                raise ValueError("LEDGER_INVALID")
            if entry.get("status") in ("reserved", "outcome_unknown", "TIMEOUT_UNKNOWN"):
                raise ValueError("LEDGER_OUTCOME_UNKNOWN")
            if entry.get("status") not in {
                "response_received",
                "PROVIDER_AUTH",
                "PROVIDER_RATE_LIMIT",
                "PROVIDER_UNAVAILABLE",
                "INVALID_OUTPUT",
            }:
                raise ValueError("LEDGER_INVALID")
            try:
                amount = Decimal(entry["reserved_usd"])
            except (KeyError, TypeError, ValueError, InvalidOperation):
                raise ValueError("LEDGER_INVALID") from None
            if not amount.is_finite() or amount < RESERVATION:
                raise ValueError("LEDGER_INVALID")
            reserved += amount
        if len(entries) > MAX_CALLS or reserved > MAX_BUDGET:
            raise ValueError("LEDGER_INVALID")

    def write(self):
        if self.lock_fd is None:
            raise ValueError("LEDGER_CLOSED")
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(self.data, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            self.write_failed = True
            temporary.unlink(missing_ok=True)
            raise

    def reserve(self, kind):
        with self.mutex:
            entries = self.data["entries"]
            reserved = sum((Decimal(x["reserved_usd"]) for x in entries), Decimal(0))
            if (
                self.write_failed
                or self.lock_fd is None
                or any(x["status"] in ("reserved", "outcome_unknown", "TIMEOUT_UNKNOWN") for x in entries)
                or len(entries) >= MAX_CALLS
                or reserved + RESERVATION > MAX_BUDGET
            ):
                raise ProviderFailure("PROVIDER_UNAVAILABLE")
            self.active = len(entries)
            entries.append(
                {
                    "number": self.active + 1,
                    "kind": kind,
                    "reserved_usd": str(RESERVATION),
                    "max_input_tokens": 1_000_000,
                    "max_output_including_reasoning_tokens": OUTPUT_CAP,
                    "status": "reserved",
                    "usage": None,
                }
            )
            self.write()

    def observe(self, usage):
        with self.mutex:
            entry = self.data["entries"][self.active]
            entry["usage"] = usage
            inputs, outputs = usage.get("input_tokens"), usage.get("output_tokens")
            if inputs is not None and outputs is not None:
                rate_in, rate_out = (
                    (Decimal("0.10"), Decimal("0.50"))
                    if inputs <= 100_000
                    else (Decimal("0.50"), Decimal("2.50"))
                )
                entry["usage_estimated_usd"] = str(
                    (inputs * rate_in + outputs * rate_out) / Decimal(1_000_000)
                )
            self.write()

    def finish(self, status):
        with self.mutex:
            self.data["entries"][self.active]["status"] = status
            self.write()

    def close(self):
        if self.lock_fd is None and not self.lock_owned:
            return
        fd, self.lock_fd = self.lock_fd, None
        try:
            if fd is not None:
                os.close(fd)
        finally:
            if self.lock_owned:
                self.lock.unlink(missing_ok=True)
                self.lock_owned = False


class MemoryCredentials:
    """Real credential bytes never enter the normal vault or a fixture file."""

    def __init__(self):
        self.items = {}

    def put(self, key):
        ref = uid()
        self.items[ref] = (key, time.monotonic() + 900)
        return ref

    def get(self, ref):
        self.purge()
        return self.items.get(ref, (None, 0))[0]

    def delete(self, ref):
        self.items.pop(ref, None)

    def purge(self):
        for ref, (_, expiry) in list(self.items.items()):
            if expiry <= time.monotonic():
                self.items.pop(ref, None)


class BudgetProvider:
    def __init__(self, ledger):
        self.ledger = ledger
        self.actual = ProviderRegistry(
            Settings(profile="test", enabled_models=(MODEL,)),
            max_completion_tokens=OUTPUT_CAP,
            usage_observer=ledger.observe,
        )

    def list_models(self):
        return self.actual.list_models()

    def ensure_model(self, model):
        return self.actual.ensure_model(model)

    def validate_key(self, model, key):
        return self.actual.validate_key(model, key)

    def generate(self, model, key, kind, payload):
        # Small synthetic source only; body bound also rules out arbitrary document exfiltration.
        if len(json.dumps(payload).encode()) > 100_000:
            raise ProviderFailure("INVALID_OUTPUT")
        self.ledger.reserve(kind)
        print(
            json.dumps({"live_call": self.ledger.active + 1, "kind": kind, "state": "reserved"}), flush=True
        )
        try:
            result = self.actual.generate(model, key, kind, payload)
        except ProviderFailure as exc:
            self.ledger.finish("outcome_unknown" if exc.outcome_unknown else exc.code)
            raise
        except Exception:
            self.ledger.finish("outcome_unknown")
            raise ProviderFailure("TIMEOUT_UNKNOWN", outcome_unknown=True) from None
        else:
            self.ledger.finish("response_received")
            print(
                json.dumps({"live_call": self.ledger.active + 1, "kind": kind, "state": "response_received"}),
                flush=True,
            )
            return result


def execute(key, ledger, directory):
    ctx = None
    credentials = MemoryCredentials()
    try:
        fixture = make_test_application(directory)
        ctx = fixture.ctx
        ctx.credentials = credentials
        ctx.providers = BudgetProvider(ledger)
        ctx.providers.validate_key(MODEL, key)
        report = {
            "model": MODEL,
            "stages": [],
            "real_key_persistence": False,
            "source": "synthetic text and generated image",
        }
        with TestClient(fixture.app) as client:
            client.headers["Authorization"] = "Bearer test-owner-token"

            def post(path, data):
                response = client.post(path, json=data, headers={"Idempotency-Key": uid(), "X-AI-Key": key})
                if response.status_code not in (201, 202):
                    raise RuntimeError(
                        "Request failed at " + path.split("/")[2] + " status=" + str(response.status_code)
                    )
                return response.json()["data"]

            def run(job):
                ctx.jobs.run_once()
                with ctx.db.transaction() as s:
                    row = s.get(Job, job["id"])
                    report["stages"].append(
                        {"kind": row.kind, "status": row.status, "error_code": row.error_code}
                    )
                    if row.status != "succeeded":
                        raise RuntimeError("Job " + row.kind + " failed: " + str(row.error_code))

            def material(name, mime, data):
                created = post(
                    "/v1/materials", {"filename": name, "mime_type": mime, "size_bytes": len(data)}
                )
                response = client.put(created["upload_url"], content=data, headers={"Content-Type": mime})
                if response.status_code != 204:
                    raise RuntimeError("Synthetic upload rejected")
                mid = created["material"]["id"]
                run(post(f"/v1/materials/{mid}/complete", {"model_id": MODEL})["job"])
                return mid

            mid = material("synthetic-physics.txt", "text/plain", SOURCE.encode())
            concepts = client.get(f"/v1/materials/{mid}/concepts").json()["data"]
            if not concepts:
                raise RuntimeError("Analysis produced no concepts")
            concept, draft = concepts[0], concepts[0]["current_version"]
            response = client.patch(
                f"/v1/materials/{mid}/concepts",
                json={
                    "concepts": [
                        {
                            "concept_id": concept["concept_id"],
                            "unit_id": concept["unit_id"],
                            "base_version_id": draft["id"],
                            "name": draft["name"],
                            "summary": draft["summary"],
                            "checklist_items": [x["criterion"] for x in draft["checklist_items"]],
                            "evidence_chunk_ids": [x["chunk_id"] for x in draft["evidence"]],
                        }
                    ]
                },
            )
            if response.status_code != 200:
                raise RuntimeError("Confirmed version failed")
            vid = response.json()["data"][0]["version"]["id"]
            job = post(
                "/v1/question-sets",
                {
                    "unit_id": concept["unit_id"],
                    "concept_version_ids": [vid],
                    "count": 2,
                    "difficulty": "easy",
                    "model_id": MODEL,
                },
            )["job"]
            run(job)
            questions = client.get("/v1/question-sets/" + job["resource_id"]).json()["data"]["items"]
            if len(questions) != 2 or any("answer_key" in x["question"] for x in questions):
                raise RuntimeError("Public question shape failed")
            job = post(
                "/v1/learning-explanations",
                {"concept_version_id": vid, "explanation_language": "ko", "model_id": MODEL},
            )["job"]
            run(job)
            explanation = client.get("/v1/learning-explanations/" + job["resource_id"]).json()["data"]
            if not explanation["content"] or explanation["concept_version_id"] != vid:
                raise RuntimeError("Explanation pin failed")
            image = Image.new("RGB", (800, 160), "white")
            ImageDraw.Draw(image).text(
                (20, 30),
                "Momentum equals mass times velocity.\nTotal momentum is conserved with zero external force.",
                fill="black",
                font_size=24,
            )
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            material("synthetic-physics.png", "image/png", buffer.getvalue())
        if ctx.credentials.items:
            raise RuntimeError("Live memory credentials were not cleaned")
        with ctx.db.transaction() as s:
            if any(x.credential_ref is not None for x in s.scalars(select(Job))):
                raise RuntimeError("Live job credential references remained")
        # Check every temporary artifact before cleanup; never emit key or matching data.
        for path in directory.rglob("*"):
            if path.is_file() and key.encode() in path.read_bytes():
                raise RuntimeError("Key persistence verification failed")
        report["status"] = "pass"
        return report
    finally:
        # Clearing the captured variable also clears nested request closures.
        key = None
        credentials.items.clear()
        if ctx is not None:
            ctx.db.engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Authorized synthetic Anthropic test; maximum shared budget USD10"
    )
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--ledger", required=True)
    args = parser.parse_args(argv)
    ledger = None
    key = None
    report = {"status": "failed", "model": MODEL}
    try:
        ledger = Ledger(args.ledger)
        with Path(args.key_file).open("rb") as handle:
            key = parse_credential_bytes(handle.read(MAX_INPUT_BYTES + 1))
        with tempfile.TemporaryDirectory(prefix="backend-live-") as temporary:
            report = execute(key, ledger, Path(temporary))
    except CredentialInputError:
        report["error"] = "CREDENTIAL_INPUT_INVALID"
    except (ValueError, RuntimeError, ApiError, ProviderFailure) as exc:
        # No raw exceptions, arbitrary messages, or provider response data.
        allowed_codes = {
            "PROVIDER_AUTH",
            "PROVIDER_RATE_LIMIT",
            "PROVIDER_UNAVAILABLE",
            "KEY_VALIDATION_UNAVAILABLE",
            "MODEL_UNAVAILABLE",
            "TIMEOUT_UNKNOWN",
            "INVALID_OUTPUT",
        }
        code = exc.code if isinstance(exc, (ApiError, ProviderFailure)) else None
        report["error"] = code if code in allowed_codes else "LIVE_CHECK_FAILED"
    except Exception:
        report["error"] = "LIVE_CHECK_FAILED"
    finally:
        key = None
        if ledger is not None:
            try:
                ledger.data["runs"].append(report)
                ledger.write()
            except Exception:
                report["status"] = "failed"
                report["error"] = "LEDGER_WRITE_FAILED"
            finally:
                try:
                    ledger.close()
                except Exception:
                    report["status"] = "failed"
                    report["error"] = "LEDGER_CLOSE_FAILED"
    # Existing ledger metadata is operator-controlled; never echo it wholesale.
    summary = (
        None
        if ledger is None
        else {
            "budget_usd": str(MAX_BUDGET),
            "generation_calls": len(ledger.data["entries"]),
            "reserved_usd": str(
                sum((Decimal(x["reserved_usd"]) for x in ledger.data["entries"]), Decimal(0))
            ),
        }
    )
    print(json.dumps({"report": report, "ledger": summary}, ensure_ascii=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
