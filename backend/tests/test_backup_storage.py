import hashlib
import io
import os
import threading
import time
from uuid import uuid4

import pytest

from app.services.backups.process import BackupAdapterError
from app.services.backups.storage import LocalStore, S3Credentials, S3Location, S3Store, artifact_filename


def test_local_atomic_copy_download_and_identity(tmp_path):
    instance, execution = str(uuid4()), str(uuid4())
    store = LocalStore(tmp_path / "backups", instance)
    source = tmp_path / "source"
    source.write_bytes(b"database snapshot" * 10000)
    filename = artifact_filename(execution, ".sqlite3")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    locator = store.save(source, execution, filename, deadline=time.monotonic() + 5,
                         stop_event=threading.Event())
    opened = store.open(locator, source.stat().st_size)
    try:
        assert hashlib.sha256(opened.read()).hexdigest() == digest
        assert os.fstat(opened.fileno()).st_mode & 0o777 == 0o600
    finally:
        opened.close()
    assert store.verify(locator, source.stat().st_size, digest)
    store.delete(locator)
    assert not store.verify(locator, source.stat().st_size, digest)


def test_local_store_never_follows_instance_or_file_symlink(tmp_path):
    instance, execution = str(uuid4()), str(uuid4())
    root = tmp_path / "backups"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / instance).symlink_to(outside, target_is_directory=True)
    with pytest.raises(BackupAdapterError):
        LocalStore(root, instance).ensure_directory()
    (root / instance).unlink()
    store = LocalStore(root, instance)
    store.ensure_directory()
    source = tmp_path / "source"
    source.write_bytes(b"safe")
    locator = store.save(source, execution, artifact_filename(execution, ".dump"),
                         deadline=time.monotonic() + 5, stop_event=threading.Event())
    path = root / instance / locator["filename"]
    path.unlink()
    path.symlink_to(source)
    with pytest.raises(BackupAdapterError):
        store.open(locator, 4)
    with pytest.raises(BackupAdapterError):
        store.delete(locator)
    assert source.read_bytes() == b"safe"


def test_local_copy_interruption_does_not_publish_or_remove_existing(tmp_path):
    instance, execution = str(uuid4()), str(uuid4())
    store = LocalStore(tmp_path / "backups", instance)
    source = tmp_path / "source"
    source.write_bytes(b"snapshot")
    event = threading.Event()
    event.set()
    with pytest.raises(BackupAdapterError):
        store.save(source, execution, artifact_filename(execution, ".dump"),
                   deadline=time.monotonic() + 5, stop_event=event)
    assert not list((tmp_path / "backups").rglob("*.dump"))
    assert source.read_bytes() == b"snapshot"


def test_local_delete_and_open_refuse_replaced_regular_file(tmp_path):
    instance, execution = str(uuid4()), str(uuid4())
    store = LocalStore(tmp_path / "backups", instance)
    source = tmp_path / "source"
    source.write_bytes(b"safe")
    locator = store.save(source, execution, artifact_filename(execution, ".dump"),
                         deadline=time.monotonic() + 5, stop_event=threading.Event())
    path = tmp_path / "backups" / instance / locator["filename"]
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"evil")
    replacement.replace(path)
    with pytest.raises(BackupAdapterError):
        store.open(locator, 4)
    with pytest.raises(BackupAdapterError):
        store.delete(locator)
    assert path.read_bytes() == b"evil"


@pytest.mark.parametrize("filename", ["../secret", "/tmp/x", "not-a-backup.dump", "a\x00.dump"])
def test_local_locator_is_not_a_general_file_path(tmp_path, filename):
    store = LocalStore(tmp_path / "backups", str(uuid4()))
    with pytest.raises(BackupAdapterError):
        store.open({"filename": filename}, 0)


def test_private_temporary_directory_does_not_remove_foreign_files(tmp_path):
    instance, execution = str(uuid4()), str(uuid4())
    store = LocalStore(tmp_path / "backups", instance)
    directory = store.temporary_directory(execution)
    assert directory.is_dir()
    assert directory.stat().st_mode & 0o777 == 0o700
    (directory / "owned.tmp").write_bytes(b"secret")
    foreign = directory.parent / "foreign"
    foreign.mkdir()
    (foreign / "keep").write_bytes(b"keep")
    store.clean_temporary(execution)
    assert not directory.exists()
    assert (foreign / "keep").read_bytes() == b"keep"


class _S3Error(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}
        super().__init__("private service message must never escape")


class _Events:
    def __init__(self):
        self.callbacks = {}

    def register(self, name, callback):
        self.callbacks[name] = callback


class _Client:
    def __init__(self, location, checksum, size):
        from types import SimpleNamespace

        self.meta = SimpleNamespace(events=_Events())
        self.location, self.checksum, self.size = location, checksum, size
        self.calls = []
        self.closed = False
        self.absent = False
        self.body = io.BytesIO(b"payload")

    def upload_file(self, filename, bucket, key, **kwargs):
        self.calls.append(("upload", filename, bucket, key, kwargs))
        self.meta.events.callbacks["after-call.s3.CreateMultipartUpload"](parsed={"UploadId": "owned-upload"})

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        if self.absent:
            raise _S3Error("NoSuchVersion")
        return {"ContentLength": self.size, "Metadata": {
            "sha256": self.checksum, "execution": self.location.execution_key,
            "instance": self.location.instance_key,
        }, "VersionId": "owned-version"}

    def get_object(self, **kwargs):
        return {**self.head_object(**kwargs), "Body": self.body}

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))
        self.absent = True

    def abort_multipart_upload(self, **kwargs):
        self.calls.append(("abort", kwargs))

    def close(self):
        self.closed = True


def _store_and_client(monkeypatch):
    from app.services.backups.network import NetworkPolicy

    instance, execution = str(uuid4()), str(uuid4())
    location = S3Location("https://s3.example.test", "us-east-1", "backups",
                          f"prefix/{instance}/{execution}/{artifact_filename(execution, '.dump')}", instance, execution)
    store = S3Store(location, S3Credentials("private-access", "private-secret"), NetworkPolicy())
    client = _Client(location, "a" * 64, 7)
    monkeypatch.setattr(store, "_client", lambda *_args: client)
    return store, client


def test_s3_bounded_managed_upload_persists_identity_and_version(tmp_path, monkeypatch):
    store, client = _store_and_client(monkeypatch)
    source = tmp_path / "payload"
    source.write_bytes(b"payload")
    uploads = []
    version = store.save(source, 7, "a" * 64, deadline=time.monotonic() + 5,
                         stop_event=threading.Event(), upload_started=uploads.append)
    assert uploads == ["owned-upload"]
    assert version == "owned-version"
    options = client.calls[0][4]
    assert options["ExtraArgs"]["Metadata"] == {
        "sha256": "a" * 64, "execution": store.location.execution_key, "instance": store.location.instance_key,
    }
    transfer = options["Config"]
    assert transfer.max_concurrency == 2
    assert transfer.max_io_queue == 4
    assert transfer.preferred_transfer_client == "classic"
    assert client.closed


def test_s3_delete_targets_exact_version_and_confirms_missing(monkeypatch):
    store, client = _store_and_client(monkeypatch)
    store.delete("owned-version", deadline=time.monotonic() + 5, stop_event=threading.Event())
    assert client.calls == [
        ("delete", {"Bucket": store.location.bucket, "Key": store.location.key, "VersionId": "owned-version"}),
        ("head", {"Bucket": store.location.bucket, "Key": store.location.key, "VersionId": "owned-version"}),
    ]
    assert client.closed


def test_s3_stream_close_also_closes_client_and_checks_metadata(monkeypatch):
    store, client = _store_and_client(monkeypatch)
    body = store.open(7, "a" * 64, "owned-version", deadline=time.monotonic() + 5, stop_event=threading.Event())
    assert not client.closed
    assert body.read(2) == b"pa"
    body.close()
    assert client.closed and client.body.closed


def test_s3_wrong_identity_never_returns_body_or_raw_error(monkeypatch):
    store, client = _store_and_client(monkeypatch)
    with pytest.raises(BackupAdapterError) as error:
        store.open(7, "b" * 64, "owned-version", deadline=time.monotonic() + 5, stop_event=threading.Event())
    assert error.value.code == "storage_identity"
    assert client.closed and client.body.closed


def test_s3_failed_upload_only_aborts_its_known_upload(tmp_path, monkeypatch):
    store, client = _store_and_client(monkeypatch)
    source = tmp_path / "payload"
    source.write_bytes(b"payload")

    def fail(*args, **kwargs):
        client.meta.events.callbacks["after-call.s3.CreateMultipartUpload"](parsed={"UploadId": "owned-upload"})
        raise _S3Error("AccessDenied")

    monkeypatch.setattr(client, "upload_file", fail)
    with pytest.raises(BackupAdapterError) as error:
        store.save(source, 7, "a" * 64, deadline=time.monotonic() + 5, stop_event=threading.Event())
    assert error.value.code == "storage_failed"
    assert "private" not in str(error.value)
    assert client.calls == [("abort", {
        "Bucket": store.location.bucket, "Key": store.location.key, "UploadId": "owned-upload",
    })]
    assert client.closed


def test_s3_client_disables_implicit_credentials_proxies_and_revalidates_each_request(monkeypatch):
    from types import SimpleNamespace

    import boto3

    store, client = _store_and_client(monkeypatch)
    seen, requests = {}, []

    def create(service, **kwargs):
        seen.update(kwargs)
        return client

    class Policy:
        ca_bundle = "/controlled/ca.pem"

        def validate_endpoint(self, endpoint):
            assert endpoint == store.location.endpoint
            return "https://s3.example.test:443"

        def validate_request(self, url, origin):
            requests.append((url, origin))
            if url.startswith("https://other"):
                raise BackupAdapterError("network_blocked")

    monkeypatch.setattr(boto3, "client", create)
    store.policy = Policy()
    client = S3Store._client(store, time.monotonic() + 5, threading.Event())
    assert seen["aws_access_key_id"] == "private-access"
    assert seen["aws_secret_access_key"] == "private-secret"
    assert seen["config"].proxies == {}
    assert seen["config"].s3["addressing_style"] == "path"
    assert seen["verify"] == "/controlled/ca.pem"
    guard = client.meta.events.callbacks["before-send.s3"]
    guard(SimpleNamespace(url="https://s3.example.test/key?uploadId=owned"))
    with pytest.raises(BackupAdapterError):
        guard(SimpleNamespace(url="https://other.example.test/key"))
    assert len(requests) == 2
