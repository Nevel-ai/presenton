"""Offline regression tests for the real boto3 client and transfer configuration."""

import asyncio
import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from boto3.s3.transfer import TransferConfig
from botocore.client import Config

from utils import s3_utils


BASE_ENV = {
    "OBJECT_STORAGE_ENDPOINT": "https://s3.example.test",
    "OBJECT_STORAGE_REGION": "test-region",
    "OBJECT_STORAGE_ACCESS_KEY_ID": "test-access",
    "OBJECT_STORAGE_SECRET_ACCESS_KEY": "test-secret",
    "OBJECT_STORAGE_BUCKET_NAME": "test-bucket",
    "OBJECT_STORAGE_PREFIX": "api/presentation/images",
}
SELECTEL_ENV = {
    "OBJECT_STORAGE_FORCE_PATH_STYLE": "false",
    "OBJECT_STORAGE_REQUEST_CHECKSUM_CALCULATION": "when_required",
    "OBJECT_STORAGE_RESPONSE_CHECKSUM_VALIDATION": "when_required",
    "OBJECT_STORAGE_TRANSFER_MAX_CONCURRENCY": "1",
    "OBJECT_STORAGE_TRANSFER_USE_THREADS": "false",
}


class S3ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, BASE_ENV, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        s3_utils._s3_client = None
        self.addCleanup(setattr, s3_utils, "_s3_client", None)

    def test_defaults_preserve_path_addressing_checksums_and_concurrent_transfers(self):
        with patch.object(s3_utils.boto3, "client") as factory:
            s3_utils._build_s3_client()

        args, kwargs = factory.call_args
        self.assertEqual(args, ("s3",))
        self.assertEqual(kwargs["endpoint_url"], BASE_ENV["OBJECT_STORAGE_ENDPOINT"])
        self.assertEqual(kwargs["region_name"], "test-region")
        self.assertEqual(kwargs["aws_access_key_id"], "test-access")
        self.assertEqual(kwargs["aws_secret_access_key"], "test-secret")
        config = kwargs["config"]
        self.assertIsInstance(config, Config)
        self.assertEqual(config.s3, {"addressing_style": "path"})
        self.assertEqual(config.request_checksum_calculation, "when_supported")
        self.assertEqual(config.response_checksum_validation, "when_supported")
        transfer = s3_utils._build_transfer_config()
        self.assertIsInstance(transfer, TransferConfig)
        self.assertEqual(transfer.max_concurrency, 10)
        self.assertTrue(transfer.use_threads)
        self.assertEqual(transfer.preferred_transfer_client, "auto")
        self.assertEqual(transfer.multipart_threshold, 8 * 1024 * 1024)
        self.assertEqual(transfer.multipart_chunksize, 8 * 1024 * 1024)

    def test_selectel_profile_reaches_the_actual_boto_client(self):
        with patch.dict(os.environ, SELECTEL_ENV):
            client = s3_utils._build_s3_client()
            try:
                config = client.meta.config
                self.assertEqual(config.s3["addressing_style"], "virtual")
                self.assertEqual(config.request_checksum_calculation, "when_required")
                self.assertEqual(config.response_checksum_validation, "when_required")
                self.assertEqual(config.region_name, "test-region")
            finally:
                client.close()

    def test_invalid_client_settings_fail_before_client_creation(self):
        cases = {
            "OBJECT_STORAGE_FORCE_PATH_STYLE": ("", "TRUE", "0", "yes", " false "),
            "OBJECT_STORAGE_REQUEST_CHECKSUM_CALCULATION": ("", "WHEN_REQUIRED", "never"),
            "OBJECT_STORAGE_RESPONSE_CHECKSUM_VALIDATION": ("", "WHEN_REQUIRED", "never"),
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                    with patch.object(s3_utils.boto3, "client") as factory:
                        with self.assertRaisesRegex(ValueError, name):
                            s3_utils._build_s3_client()
                        factory.assert_not_called()

    def test_missing_configuration_disables_s3_without_creating_a_client(self):
        for name in ("OBJECT_STORAGE_ENDPOINT", "OBJECT_STORAGE_ACCESS_KEY_ID", "OBJECT_STORAGE_SECRET_ACCESS_KEY"):
            with self.subTest(name=name), patch.dict(os.environ, {name: ""}):
                with patch.object(s3_utils.boto3, "client") as factory:
                    self.assertIsNone(s3_utils._build_s3_client())
                    factory.assert_not_called()

    def test_client_is_cached(self):
        with patch.object(s3_utils, "_build_s3_client") as factory:
            self.assertIs(s3_utils.get_s3_client(), s3_utils.get_s3_client())
            factory.assert_called_once_with()


class S3TransferTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, BASE_ENV, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.client = Mock()
        self.client_patch = patch.object(s3_utils, "get_s3_client", return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_upload_preserves_key_and_mime_with_selected_transfer_settings(self):
        for profile, concurrency, threads in (({}, 10, True), (SELECTEL_ENV, 1, False)):
            with self.subTest(profile=profile), patch.dict(os.environ, profile):
                path = os.path.join(self.directory.name, "asset.png")
                key = s3_utils._upload_file_to_s3_sync(path, "/presentation-id/")
                self.assertEqual(key, "api/presentation/images/presentation-id/asset.png")
                args, kwargs = self.client.upload_file.call_args
                self.assertEqual(args, (path, "test-bucket", key))
                self.assertEqual(kwargs["ExtraArgs"], {"ContentType": "image/png"})
                transfer = kwargs["Config"]
                self.assertIsInstance(transfer, TransferConfig)
                self.assertEqual(transfer.max_concurrency, concurrency)
                self.assertIs(transfer.use_threads, threads)
                self.assertEqual(transfer.preferred_transfer_client, "auto" if threads else "classic")

    def test_upload_retains_optional_prefix_postfix_and_unknown_mime(self):
        with patch.dict(os.environ, {"OBJECT_STORAGE_PREFIX": ""}):
            key = s3_utils._upload_file_to_s3_sync("/tmp/asset.nevel-unknown")
        self.assertEqual(key, "asset.nevel-unknown")
        self.assertIsNone(self.client.upload_file.call_args.kwargs["ExtraArgs"])

    def test_download_creates_destination_directory_and_passes_transfer_config(self):
        path = os.path.join(self.directory.name, "nested", "asset.png")
        with patch.dict(os.environ, SELECTEL_ENV):
            result = s3_utils._download_file_from_s3_sync("opaque/key.png", path)
        self.assertEqual(result, path)
        self.assertTrue(os.path.isdir(os.path.dirname(path)))
        args, kwargs = self.client.download_file.call_args
        self.assertEqual(args, ("test-bucket", "opaque/key.png", path))
        self.assertEqual(kwargs["Config"].max_concurrency, 1)
        self.assertFalse(kwargs["Config"].use_threads)
        self.assertEqual(kwargs["Config"].preferred_transfer_client, "classic")

    def test_invalid_transfer_settings_never_start_upload_or_download(self):
        cases = {
            "OBJECT_STORAGE_TRANSFER_MAX_CONCURRENCY": ("", "0", "-1", "1.5", "1e2", " 1", "١"),
            "OBJECT_STORAGE_TRANSFER_USE_THREADS": ("", "TRUE", "0", "yes", " false "),
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                    with self.assertRaisesRegex(ValueError, name):
                        s3_utils._upload_file_to_s3_sync("/tmp/asset.png")
                    with self.assertRaisesRegex(ValueError, name):
                        s3_utils._download_file_from_s3_sync("asset.png", os.path.join(self.directory.name, "asset.png"))
        self.client.upload_file.assert_not_called()
        self.client.download_file.assert_not_called()

    def test_missing_bucket_or_client_keeps_disabled_fallback(self):
        path = os.path.join(self.directory.name, "asset.png")
        with patch.dict(os.environ, {"OBJECT_STORAGE_BUCKET_NAME": ""}):
            self.assertIsNone(s3_utils._upload_file_to_s3_sync(path))
            self.assertIsNone(s3_utils._download_file_from_s3_sync("asset.png", path))
        with patch.object(s3_utils, "get_s3_client", return_value=None):
            self.assertIsNone(s3_utils._upload_file_to_s3_sync(path))
            self.assertIsNone(s3_utils._download_file_from_s3_sync("asset.png", path))
        self.client.upload_file.assert_not_called()
        self.client.download_file.assert_not_called()

    def test_async_wrappers_preserve_key_and_path(self):
        path = os.path.join(self.directory.name, "asset.png")
        self.assertEqual(asyncio.run(s3_utils.upload_file_to_s3(path, "presentation-id")), "api/presentation/images/presentation-id/asset.png")
        self.assertEqual(asyncio.run(s3_utils.download_file_from_s3("asset.png", path)), path)

    def test_async_failure_falls_back_without_logging_signed_urls(self):
        error = RuntimeError("https://bucket.example/key?X-Amz-Signature=secret")
        self.client.upload_file.side_effect = error
        self.client.download_file.side_effect = error
        output = io.StringIO()
        path = os.path.join(self.directory.name, "asset.png")
        with contextlib.redirect_stdout(output):
            self.assertIsNone(asyncio.run(s3_utils.upload_file_to_s3(path)))
            self.assertIsNone(asyncio.run(s3_utils.download_file_from_s3("asset.png", path)))
        self.assertEqual(output.getvalue(), "Error uploading file to S3 (RuntimeError)\nError downloading file from S3 (RuntimeError)\n")


if __name__ == "__main__":
    unittest.main()
