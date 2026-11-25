"""AI 用量、计价和预算的传输契约。"""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
    model_validator,
)

from app.schemas.ai_config import ModelType, ProviderName

Currency = Literal["CNY", "USD"]
CURRENCIES = ("CNY", "USD")
UsageSource = Literal["provider", "estimated", "unknown"]
UsageStatus = Literal["pending", "succeeded", "failed", "cancelled", "interrupted"]
PricingStatus = Literal["pending", "priced", "missing_price", "missing_usage", "unconfirmed", "private"]
PricePeriod = Literal["fixed", "peak", "off_peak"]
BudgetState = Literal["normal", "near", "exceeded"]
UsageScope = Literal["account", "project"]
MoneyInput = Annotated[str, StringConstraints(strict=True, pattern=r"^\d{1,10}(?:\.\d{1,9})?$")]
LocalClockTime = Annotated[str, StringConstraints(strict=True, pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
EntityId = Annotated[int, Field(gt=0, le=2_147_483_647)]


class UsageFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project_id: EntityId | None = None
    chapter_id: EntityId | None = None
    config_id: EntityId | None = None
    provider: ProviderName | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    kind: str | None = Field(default=None, min_length=1, max_length=100)
    model_type: ModelType | None = None
    status: UsageStatus | None = None
    from_at: AwareDatetime | None = Field(default=None, alias="from")
    to_at: AwareDatetime | None = Field(default=None, alias="to")
    page: int = Field(default=1, ge=1, le=10_000_000)
    page_size: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        try:
            if self.from_at is not None:
                self.from_at = self.from_at.astimezone(UTC)
            if self.to_at is not None:
                self.to_at = self.to_at.astimezone(UTC)
        except OverflowError:
            raise ValueError("时间超出可查询范围") from None
        if self.from_at is not None and self.to_at is not None and self.from_at >= self.to_at:
            raise ValueError("开始时间必须早于结束时间")
        return self


class AdminUsageFilters(UsageFilters):
    actor_id: EntityId | None = None
    payer_id: EntityId | None = None


class UsageRow(BaseModel):
    id: str
    started_at: datetime
    actor_id: int | None
    actor_name: str | None
    payer_id: int | None
    payer_name: str | None
    project_id: int | None
    project_title: str | None
    chapter_id: int | None
    task_id: int | None
    provider: str | None
    model: str | None
    kind: str
    model_type: ModelType
    status: UsageStatus
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    image_count: int | None
    input_source: UsageSource
    output_source: UsageSource
    image_source: UsageSource
    currency: Currency | None
    estimated_cost: str | None
    input_per_million: str | None
    cached_input_per_million: str | None
    output_per_million: str | None
    per_image: str | None
    price_period: PricePeriod | None = None
    pricing_status: PricingStatus
    billing_source: Literal["own", "author", "other", "unknown"]
    project_state: Literal["linked", "none", "deleted", "private"]


class UsageCurrencyTotal(BaseModel):
    currency: Currency
    amount: str
    calls: int


class UsageSummary(BaseModel):
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    image_count: int
    unknown_usage_calls: int
    unpriced_calls: int
    costs: list[UsageCurrencyTotal]


class UsagePage(BaseModel):
    items: list[UsageRow]
    total: int
    page: int
    page_size: int
    tracking_since: datetime | None
    summary: UsageSummary


class ModelOffPeakWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: LocalClockTime
    end: LocalClockTime


class ModelOffPeakPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)] = "Asia/Shanghai"
    start: LocalClockTime
    end: LocalClockTime
    input_per_million: MoneyInput | None = None
    cached_input_per_million: MoneyInput | None = None
    output_per_million: MoneyInput | None = None
    per_image: MoneyInput | None = None
    # 每个空闲区间最短一分钟，第一条单独存于 start、end。
    additional_windows: list[ModelOffPeakWindow] = Field(default_factory=list, max_length=1439)

    @model_serializer(mode="wrap")
    def _serialize(self, serialize: SerializerFunctionWrapHandler):
        data = serialize(self)
        if "additional_windows" not in self.model_fields_set:
            data.pop("additional_windows", None)
        return data


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: Currency
    input_per_million: MoneyInput | None = None
    cached_input_per_million: MoneyInput | None = None
    output_per_million: MoneyInput | None = None
    per_image: MoneyInput | None = None
    off_peak: ModelOffPeakPrice | None = None


class ModelPriceOut(BaseModel):
    version: str
    provider: ProviderName
    model: str
    model_type: ModelType
    price: ModelPrice | None


class ModelPriceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    price: ModelPrice | None


class BudgetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: MoneyInput
    threshold_percent: Annotated[int, Field(strict=True, ge=1, le=100)]


class BudgetProgress(BaseModel):
    instance_key: str
    scope: UsageScope
    currency: Currency
    limit: str
    threshold_percent: int
    spent: str
    unpriced_calls: int
    state: BudgetState
    period_start: datetime | None
    period_end: datetime | None
