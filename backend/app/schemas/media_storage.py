from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.schemas.backup import BackupConfiguration


class LocalLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["local"]


class S3Location(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    kind: Literal["s3"]
    endpoint: str = Field(min_length=1, max_length=2048)
    region: str = Field(default="us-east-1", min_length=1, max_length=100)
    bucket: str = Field(min_length=1, max_length=63)
    prefix: str = Field(default="", max_length=512)
    path_style: bool = True
    access_key: SecretStr | None = Field(default=None, min_length=1, max_length=2048, repr=False)
    secret_key: SecretStr | None = Field(default=None, min_length=1, max_length=2048, repr=False)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        return BackupConfiguration.valid_endpoint(value)

    @field_validator("bucket")
    @classmethod
    def validate_bucket(cls, value: str) -> str:
        return BackupConfiguration.valid_bucket(value)

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        return BackupConfiguration.valid_prefix(value)

    @field_validator("region")
    @classmethod
    def validate_region(cls, value: str) -> str:
        return BackupConfiguration.protocol_text(value)


LocationInput = Annotated[LocalLocation | S3Location, Field(discriminator="kind")]


class SavedLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    location_id: int = Field(ge=1, le=2_147_483_647)


class StoragePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=0, le=2_147_483_647)
    default_quota_bytes: int | None = Field(default=None, ge=1, le=2**53 - 1)
    location: LocationInput | None = None

    @model_validator(mode="after")
    def has_changes(self):
        if self.default_quota_bytes is None and self.location is None:
            raise ValueError("请提供配额或存储位置")
        return self


class LocationOut(BaseModel):
    id: int
    kind: Literal["local", "s3"]
    state: Literal["active", "readonly", "retired"]
    configuration: dict[str, str | bool]
    has_access_key: bool
    has_secret_key: bool
    file_count: int
    size_bytes: int


class VolumeReportOut(BaseModel):
    worker_key: str
    volume_id: str
    seen_at: datetime


class StorageOut(BaseModel):
    revision: int
    default_quota_bytes: int
    total_bytes: int
    locations: list[LocationOut]
    volume_mismatch: bool
    volume_reports: list[VolumeReportOut]
