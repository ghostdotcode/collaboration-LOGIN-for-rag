from typing import List, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

# bcrypt only reads the first 72 *bytes*; bcrypt>=5 raises on longer input, which
# used to surface as an unhandled 500 on signup/login.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_CHARS = 8


class UserSignupSchema(BaseModel):
    first_name: str = Field(min_length=1, max_length=50)
    last_name: str = Field(min_length=1, max_length=50)
    email_address: EmailStr  # Automatically validates the @ symbol and domain
    password: str = Field(min_length=MIN_PASSWORD_CHARS)
    confirm_password: str

    @field_validator("first_name", "last_name")
    @classmethod
    def _strip_names(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("email_address")
    @classmethod
    def _lower_email(cls, value: str) -> str:
        # "Alice@X.com" and "alice@x.com" are the same mailbox; without folding
        # they were two accounts and logins were case-sensitive.
        return value.lower()

    @field_validator("password")
    @classmethod
    def _password_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
            raise ValueError(f"must be at most {MAX_PASSWORD_BYTES} bytes")
        return value

    @model_validator(mode="after")
    def check_passwords_match(self) -> "UserSignupSchema":
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self


class UserLoginSchema(BaseModel):
    email_address: EmailStr
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email_address")
    @classmethod
    def _lower_email(cls, value: str) -> str:
        return value.lower()


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_name: str


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class AskRequest(BaseModel):
    query: str = Field(max_length=5000)  # tighter, configurable limit enforced in the engine
    history: List[Turn] = Field(default_factory=list, max_length=12)
