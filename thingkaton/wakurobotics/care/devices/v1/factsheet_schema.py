# generated by datamodel-codegen:
#   filename:  factsheet.schema.json
#   timestamp: 2025-06-12T11:51:29+00:00

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class DeviceFactsheet(BaseModel):
    serial: str = Field(..., description='device identification')
    name: str = Field(..., description='device name')
    manufacturer: str = Field(..., description='manufacturer slug')
    model: str = Field(..., description='model slug')
    version: Optional[str] = Field(None, description='model version')
    deployment: Optional[str] = Field(None, description='deployment name')
