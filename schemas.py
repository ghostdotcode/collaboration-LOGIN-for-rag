from pydantic import BaseModel, EmailStr, model_validator

class UserSignupSchema(BaseModel):
    first_name: str
    last_name: str
    email_address: EmailStr  # Automatically validates the @ symbol and domain
    password: str
    confirm_password: str

    @model_validator(mode='after')
    def check_passwords_match(self) -> 'UserSignupSchema':
        if self.password != self.confirm_password:
            raise ValueError('Passwords do not match')
        return self


class UserLoginSchema(BaseModel):
    email_address: EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_name: str
