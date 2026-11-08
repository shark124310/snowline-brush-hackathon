from typing import Literal

import math

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RecordInfo(BaseModel):
    writer_id: str = Field(default="writer-01", min_length=1, max_length=64)
    mount_id: str = Field(default="mount-01", min_length=1, max_length=64)
    mode: Literal["practice", "template", "validation", "test"] = "practice"
    label: str | None = Field(default=None, max_length=8)
    title: str = Field(default="一次书写", max_length=100)


class Sample(BaseModel):
    # 保留硬件组尚在开发的字段；其单位和算法版本需另行约定。
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    sample_us: int = Field(ge=0)
    ax_g: float
    ay_g: float
    az_g: float
    gx_dps: float
    gy_dps: float
    gz_dps: float
    seq: int | None = None
    boot_id: str | int | None = None
    roll_deg: float | None = None
    pitch_deg: float | None = None
    yaw_deg: float | None = None
    distance_mm: float | None = None

    @model_validator(mode="after")
    def flat_extra_fields(self):
        for value in (self.model_extra or {}).values():
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise ValueError("附加字段请使用单个数值或文字。")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("附加字段不能包含 NaN 或无穷值。")
        return self


class Batch(BaseModel):
    samples: list[Sample] = Field(min_length=1, max_length=1000)


class Scope(BaseModel):
    writer_id: str = "writer-01"
    mount_id: str = "mount-01"
    source_kind: Literal["real", "synthetic"] = "real"


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
