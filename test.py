from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class SignupRequest(BaseModel):
    first_name: str
    last_name: str
    email: str
    password: str
    confirm_password: str


@app.post("/signup")
def signup(user: SignupRequest):
    return {
        "message": "Signup successful",
        "user": user
    }