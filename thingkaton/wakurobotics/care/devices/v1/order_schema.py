# generated by datamodel-codegen:
#   filename:  order.schema.json
#   timestamp: 2025-06-12T11:51:29+00:00

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class Status(Enum):
    pending = 'pending'
    started = 'started'
    finished = 'finished'
    cancelled = 'cancelled'
    aborted = 'aborted'


class DeviceOrder(BaseModel):
    timestamp: datetime = Field(
        ...,
        description='Timestamp in ISO8601 format (YYYY-MM-DDTHH:mm:ss.ssZ).',
        examples=['1991-03-11T11:40:03.12Z'],
    )
    name: Optional[str] = Field(None, description='order name')
    id: str = Field(..., description='unique order identification')
    device_order_id: Optional[str] = Field(
        None, description='device-internal order identification'
    )
    status: Status = Field(..., description='order status')
    parameters: Optional[Dict[str, Any]] = Field(
        None,
        description='parameters to the current mission as key value pairs of any type',
    )
