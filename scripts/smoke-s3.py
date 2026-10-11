"""Explicitly opted-in Selectel smoke using only uniquely owned synthetic objects.

Run with OBJECT_STORAGE_* credentials/profile in the child process environment:
    python scripts/smoke-s3.py --live --report /tmp/presenton-s3.json
No credentials, request URLs or raw provider errors are written to the report.
"""

import argparse
from collections import Counter
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "servers" / "fastapi"))

from utils import s3_utils


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    required = {
        "OBJECT_STORAGE_ENDPOINT": "https://s3.kz-1.srvstorage.kz",
        "OBJECT_STORAGE_REGION": "kz-1",
        "OBJECT_STORAGE_BUCKET_NAME": "prod-nevel-s3",
        "OBJECT_STORAGE_FORCE_PATH_STYLE": "false",
        "OBJECT_STORAGE_REQUEST_CHECKSUM_CALCULATION": "when_required",
        "OBJECT_STORAGE_RESPONSE_CHECKSUM_VALIDATION": "when_required",
        "OBJECT_STORAGE_TRANSFER_MAX_CONCURRENCY": "1",
        "OBJECT_STORAGE_TRANSFER_USE_THREADS": "false",
    }
    for name, expected in required.items():
        if os.getenv(name) != expected:
            parser.error(f"{name} must match the explicit Selectel smoke profile")

    prefix = f"migration-smoke/DEV-1528-{uuid.uuid4()}/"
    os.environ["OBJECT_STORAGE_PREFIX"] = prefix
    bucket = required["OBJECT_STORAGE_BUCKET_NAME"]
    report = {
        "prefix": prefix,
        "versions": {name: version(name) for name in ("boto3", "botocore", "s3transfer", "jmespath", "python-dateutil", "six", "urllib3")},
        "profile": required,
        "tests": [],
        "cleanup": {},
        "passed": False,
    }
    client = s3_utils.get_s3_client()
    operations = Counter()

    def count_request(model, **_kwargs):
        operations[model.name] += 1

    client.meta.events.register("before-call.s3", count_request)
    owned_keys = []

    def deadline(_signum, _frame):
        raise TimeoutError("S3 smoke operation deadline exceeded")

    signal.signal(signal.SIGALRM, deadline)
    try:
        with tempfile.TemporaryDirectory(prefix="presenton-s3-") as directory:
            cases = (
                ("small.txt", 4096, "text/plain"),
                ("multipart.pptx", 10485883, "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            )
            for filename, size, content_type in cases:
                payload = (bytes(range(256)) * ((size + 255) // 256))[:size]
                source = Path(directory) / filename
                source.write_bytes(payload)
                key = f"{prefix}presenton/{filename}"
                owned_keys.append(key)
                before = operations.copy()
                signal.alarm(240)
                uploaded = s3_utils._upload_file_to_s3_sync(str(source), "presenton")
                assert uploaded == key, "returned key changed"
                metadata = client.head_object(Bucket=bucket, Key=key)
                destination = Path(directory) / "download" / filename
                downloaded = s3_utils._download_file_from_s3_sync(key, str(destination))
                signal.alarm(0)
                assert downloaded == str(destination), "download failed"
                assert metadata["ContentLength"] == size, "object size changed"
                assert metadata["ContentType"] == content_type, "MIME changed"
                assert hashlib.sha256(destination.read_bytes()).digest() == hashlib.sha256(payload).digest(), "bytes changed"
                used = dict(operations - before)
                if filename == "multipart.pptx":
                    assert used.get("CreateMultipartUpload") == 1, "multipart was not used"
                    assert used.get("UploadPart") == 2, "unexpected part count"
                    assert used.get("CompleteMultipartUpload") == 1, "multipart did not complete"
                report["tests"].append({"file": filename, "bytes": size, "mime": content_type, "sha256_match": True, "operations": used})
            report["passed"] = True
    except Exception as error:
        # Exception text can include signed URLs; record the class only.
        report["failure_type"] = type(error).__name__
    finally:
        signal.alarm(0)
        try:
            signal.alarm(180)
            # Sweep only the fresh UUID prefix owned by this process. Register keys
            # before upload so failed PutObject outcomes are also cleaned up.
            for page in client.get_paginator("list_multipart_uploads").paginate(Bucket=bucket, Prefix=prefix):
                for upload in page.get("Uploads", []):
                    if upload["Key"] in owned_keys:
                        client.abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])
            for key in owned_keys:
                client.delete_object(Bucket=bucket, Key=key)
            remaining_objects = sum(len(page.get("Contents", [])) for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix))
            remaining_uploads = sum(len(page.get("Uploads", [])) for page in client.get_paginator("list_multipart_uploads").paginate(Bucket=bucket, Prefix=prefix))
            report["cleanup"] = {"objects": remaining_objects, "multipart_uploads": remaining_uploads}
            report["passed"] = report["passed"] and remaining_objects == 0 and remaining_uploads == 0
        except Exception as error:
            report["cleanup"] = {"failure_type": type(error).__name__}
            report["passed"] = False
        finally:
            signal.alarm(0)
            client.close()
            report["operations"] = dict(operations)
            args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "tests": len(report["tests"]), "cleanup": report["cleanup"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
