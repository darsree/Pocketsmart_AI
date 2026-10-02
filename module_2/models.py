"""PocketSmart AI - Module 2: input/output schemas (models.py).

Everything the backend validates lives here: auth models, session model and the
three planner input models (Home / Party / Jewelry).
"""
import math
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


MAX_BUDGET = 1_000_000_000


class _Base(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("total_budget", check_fields=False)
    @classmethod
    def _valid_budget(cls, v: float) -> float:
        """Friendly messages for negative / zero / nonsense budgets (applies to all planners)."""
        if not math.isfinite(v):
            raise ValueError("Please enter a valid budget amount.")
        if v < 0:
            raise ValueError("Budget cannot be negative. Please enter an amount greater than 0.")
        if v == 0:
            raise ValueError("Budget cannot be zero. Please enter an amount greater than 0.")
        if v > MAX_BUDGET:
            raise ValueError(f"Budget is too large (maximum is Rs {MAX_BUDGET:,}).")
        return v


# --------------------------------------------------------------------------- auth
class RegisterUser(_Base):
    username: str = Field(min_length=3, max_length=30, pattern=r"^[A-Za-z0-9_.-]+$")
    email: EmailStr
    full_name: Optional[str] = Field(default=None, max_length=80)
    password: str = Field(min_length=6, max_length=72)
    confirm_password: Optional[str] = None  # optional: checked only when the form sends it

    @model_validator(mode="after")
    def _passwords_match(self):
        if self.confirm_password is not None and self.confirm_password != self.password:
            raise ValueError("Passwords do not match")
        return self


class UserInDB(BaseModel):
    username: str
    email: str
    full_name: Optional[str] = None
    hashed_password: str
    disabled: bool = False
    created_at: str = ""


class Token(BaseModel):
    access_token: str
    token_type: str


class UserSession(BaseModel):
    username: str
    login_time: datetime
    last_activity: datetime
    token: str
    user_data: Dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ planner inputs


class HomeBudgetInput(_Base):
    total_budget: float
    num_lights: int = Field(default=0, ge=0, le=500)
    num_fans: int = Field(default=0, ge=0, le=500)
    num_furniture: int = Field(default=0, ge=0, le=500)
    num_dining_tables: int = Field(default=0, ge=0, le=500)
    has_living_room: bool = False
    has_kitchen: bool = False
    has_bedroom: bool = False
    additional_requirements: Optional[str] = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _something_requested(self):
        counts = self.num_lights + self.num_fans + self.num_furniture + self.num_dining_tables
        rooms = self.has_living_room or self.has_kitchen or self.has_bedroom
        if counts == 0 and not rooms:
            raise ValueError("Request at least one item (lights, fans, furniture, dining tables) or select a room")
        return self

    @property
    def rooms(self) -> List[str]:
        out = []
        if self.has_living_room:
            out.append("Living room")
        if self.has_kitchen:
            out.append("Kitchen")
        if self.has_bedroom:
            out.append("Bedroom")
        return out


class PartyBudgetInput(_Base):
    total_budget: float
    party_type: str = Field(min_length=2, max_length=50)
    num_guests: int = Field(ge=1, le=10_000)
    venue_type: Optional[str] = Field(default=None, max_length=60)
    needs_catering: bool = True
    needs_decoration: bool = True
    needs_entertainment: bool = True
    additional_requirements: Optional[str] = Field(default=None, max_length=1000)


class JewelryBudgetInput(_Base):
    total_budget: float
    occasion: str = Field(min_length=2, max_length=60)
    preferences: Optional[str] = Field(default=None, max_length=500)