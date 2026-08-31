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

loginForm.addEventListener(
    "submit",
    function (event) {

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

            formMessage.textContent =
                "Please fix the errors above.";

            formMessage.classList.add(
                "error-message"
            );

            return;
        }


        // Disable button
        loginButton.disabled = true;

        loginButton.textContent = "Logging in...";


        // Simulate login request
        setTimeout(function () {

            formMessage.textContent =
                "Login successful!";

            formMessage.classList.add(
                "success"
            );


            loginButton.disabled = false;

            loginButton.textContent = "Login";


            // Remember email
            if (rememberCheckbox.checked) {

                localStorage.setItem(
                    "rememberedEmail",
                    emailInput.value.trim()
                );

            } else {

                localStorage.removeItem(
                    "rememberedEmail"
                );

            }

        }, 1000);

    }
);


// =========================
// FORGOT PASSWORD
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
