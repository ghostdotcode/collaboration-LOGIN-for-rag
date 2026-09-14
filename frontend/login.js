// =========================
// GET HTML ELEMENTS
// =========================

const loginForm = document.getElementById("loginForm");

const emailInput = document.getElementById("email");
const passwordInput = document.getElementById("password");

const emailError = document.getElementById("emailError");
const passwordError = document.getElementById("passwordError");

const togglePassword = document.getElementById("togglePassword");

const loginButton = document.getElementById("loginButton");

const formMessage = document.getElementById("formMessage");

const rememberCheckbox = document.getElementById("remember");

const forgotPassword = document.getElementById("forgotPassword");


// SHOW / HIDE PASSWORD

togglePassword.addEventListener("click", function () {

    if (passwordInput.type === "password") {

        passwordInput.type = "text";

        togglePassword.textContent = "Hide";

        togglePassword.setAttribute(
            "aria-label",
            "Hide password"
        );

    } else {

        passwordInput.type = "password";

        togglePassword.textContent = "Show";

        togglePassword.setAttribute(
            "aria-label",
            "Show password"
        );
    }

});


// EMAIL VALIDATION

function validateEmail() {

    const email = emailInput.value.trim();

    const emailPattern =
        /^[^\s@]+@[^\s@]+\.[^\s@]+$/;


    if (email === "") {

        emailError.textContent =
            "Email is required.";

        return false;
    }


    if (!emailPattern.test(email)) {

        emailError.textContent =
            "Please enter a valid email.";

        return false;
    }


    emailError.textContent = "";

    return true;
}


// =========================
// PASSWORD VALIDATION
// =========================

function validatePassword() {

    const password = passwordInput.value;


    if (password === "") {

        passwordError.textContent =
            "Password is required.";

        return false;
    }


    if (password.length < 8) {

        passwordError.textContent =
            "Password must be at least 8 characters.";

        return false;
    }


    passwordError.textContent = "";

    return true;
}


// =========================
// REAL-TIME VALIDATION
// =========================

emailInput.addEventListener(
    "input",
    function () {

        if (emailInput.value.trim() !== "") {
            validateEmail();
        }

    }
);


passwordInput.addEventListener(
    "input",
    function () {

        if (passwordInput.value !== "") {
            validatePassword();
        }

    }
);


// =========================
// LOGIN FORM
// =========================

// =========================
// LOGIN FORM
// =========================

loginForm.addEventListener("submit", function (event) {

    // Prevent page refresh
    event.preventDefault();

    // Clear previous message
    formMessage.textContent = "";
    formMessage.className = "form-message";

    // Validate inputs
    const validEmail = validateEmail();
    const validPassword = validatePassword();

    // Stop if validation fails
    if (!validEmail || !validPassword) {
        formMessage.textContent = "Please fix the errors above.";
        formMessage.classList.add("error-message");
        return;
    }

    // Disable button
    loginButton.disabled = true;
    loginButton.textContent = "Logging in...";

    // Build the payload matching your FastAPI schema
    const payload = {
        email_address: emailInput.value.trim(),
        password: passwordInput.value
    };

    // Real login request to your FastAPI backend
    // Real login request to your FastAPI backend
    fetch("http://localhost:8000/login", {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify(payload)
    })
        .then(async response => {
            // If the response is not 200 OK, we need to carefully parse the error
            if (!response.ok) {
                const errorText = await response.text(); // Get raw text first to avoid crashes
                let errorMessage = "Invalid email or password.";

                try {
                    const errorData = JSON.parse(errorText);
                    // FastAPI 401s usually send {"detail": "message"}
                    if (typeof errorData.detail === 'string') {
                        errorMessage = errorData.detail;
                    }
                    // FastAPI 422 Validation errors send an array of missing fields
                    else if (Array.isArray(errorData.detail)) {
                        errorMessage = "Invalid input format. Please check your details.";
                        console.error("FastAPI Validation Error:", errorData.detail);
                    }
                } catch (e) {
                    console.error("Backend sent non-JSON error:", errorText);
                }

                throw new Error(errorMessage);
            }

            return response.json();
        })
        .then(data => {
            // Success! Save JWT token to local storage for the chat API
            localStorage.setItem("access_token", data.access_token);
            if (data.user_name) {
                localStorage.setItem("user_name", data.user_name);
            }

            if (rememberCheckbox.checked) {
                localStorage.setItem("rememberedEmail", emailInput.value.trim());
            } else {
                localStorage.removeItem("rememberedEmail");
            }

            formMessage.textContent = "Login successful! Redirecting...";
            formMessage.style.color = "green";

            setTimeout(() => {
                window.location.href = "index.html";
            }, 1000);
        })
        .catch(error => {
            // Force the error message to display in red and stop the fields from blanking out
            console.error("Login failed:", error.message);
            formMessage.textContent = error.message;
            formMessage.style.color = "red";

            loginButton.disabled = false;
            loginButton.textContent = "Login";
        });
});// FORGOT PASSWORD
// =========================

forgotPassword.addEventListener(
    "click",
    function (event) {

        event.preventDefault();

        formMessage.textContent =
            "Password reset functionality will be added later.";

        formMessage.className =
            "form-message error-message";

    }
);


// =========================
// LOAD REMEMBERED EMAIL
// =========================

window.addEventListener(
    "DOMContentLoaded",
    function () {

        const rememberedEmail =
            localStorage.getItem("rememberedEmail");


        if (rememberedEmail) {

            emailInput.value = rememberedEmail;

            rememberCheckbox.checked = true;
        }

    }
);
