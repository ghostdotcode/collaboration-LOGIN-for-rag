const signupForm = document.getElementById("signupForm");

const firstName = document.getElementById("firstName");
const lastName = document.getElementById("lastName");
const email = document.getElementById("email");
const password = document.getElementById("password");
const confirmPassword = document.getElementById("confirmPassword");
const terms = document.getElementById("terms");

const successMessage = document.getElementById("successMessage");


// =========================
// Helper Functions
// =========================

function showError(input, errorElement, message) {
    input.classList.add("input-error");
    errorElement.textContent = message;
}

function clearError(input, errorElement) {
    input.classList.remove("input-error");
    errorElement.textContent = "";
}


// =========================
// Email Validation
// =========================

function isValidEmail(emailValue) {
    const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

    return emailPattern.test(emailValue);
}


// =========================
// Password Validation
// =========================

function isValidPassword(passwordValue) {

    // At least 8 characters
    // At least one letter
    // At least one number

    const passwordPattern = /^(?=.*[A-Za-z])(?=.*\d).{8,}$/;

    return passwordPattern.test(passwordValue);
}


// =========================
// Form Submit
// =========================

signupForm.addEventListener("submit", async function (event) {

    // Prevent actual form submission.
    event.preventDefault();

    let isValid = true;

    successMessage.textContent = "";


    // =========================
    // Get Error Elements
    // =========================

    const firstNameError =
        document.getElementById("firstNameError");

    const lastNameError =
        document.getElementById("lastNameError");

    const emailError =
        document.getElementById("emailError");

    const passwordError =
        document.getElementById("passwordError");

    const confirmPasswordError =
        document.getElementById("confirmPasswordError");

    const termsError =
        document.getElementById("termsError");


    // =========================
    // First Name
    // =========================

    if (firstName.value.trim() === "") {

        showError(
            firstName,
            firstNameError,
            "First name is required."
        );

        isValid = false;

    } else {

        clearError(
            firstName,
            firstNameError
        );
    }


    // =========================
    // Last Name
    // =========================

    if (lastName.value.trim() === "") {

        showError(
            lastName,
            lastNameError,
            "Last name is required."
        );

        isValid = false;

    } else {

        clearError(
            lastName,
            lastNameError
        );
    }


    // =========================
    // Email
    // =========================

    const emailValue = email.value.trim();

    if (emailValue === "") {

        showError(
            email,
            emailError,
            "Email address is required."
        );

        isValid = false;

    } else if (!isValidEmail(emailValue)) {

        showError(
            email,
            emailError,
            "Please enter a valid email address."
        );

        isValid = false;

    } else {

        clearError(
            email,
            emailError
        );
    }


    // =========================
    // Password
    // =========================

    if (password.value === "") {

        showError(
            password,
            passwordError,
            "Password is required."
        );

        isValid = false;

    } else if (!isValidPassword(password.value)) {

        showError(
            password,
            passwordError,
            "Password must be at least 8 characters and contain a letter and number."
        );

        isValid = false;

    } else {

        clearError(
            password,
            passwordError
        );
    }


    // =========================
    // Confirm Password
    // =========================

    if (confirmPassword.value === "") {

        showError(
            confirmPassword,
            confirmPasswordError,
            "Please confirm your password."
        );

        isValid = false;

    } else if (password.value !== confirmPassword.value) {

        showError(
            confirmPassword,
            confirmPasswordError,
            "Passwords do not match."
        );

        isValid = false;

    } else {

        clearError(
            confirmPassword,
            confirmPasswordError
        );
    }


    // =========================
    // Terms & Conditions
    // =========================

    if (!terms.checked) {

        termsError.textContent =
            "You must accept the Terms and Conditions.";

        isValid = false;

    } else {

        termsError.textContent = "";
    }


    // =========================
    // Send Data To FastAPI
    // =========================

    if (isValid) {

        // Create JavaScript object
        const signupData = {
            first_name: firstName.value.trim(),
            last_name: lastName.value.trim(),
            email: emailValue,
            password: password.value,
            confirm_password: confirmPassword.value
        };


        // Convert JavaScript object to JSON
        const jsonData = JSON.stringify(signupData);

        console.log("JSON being sent to backend:");
        console.log(jsonData);


        try {

            // Send JSON to FastAPI signup endpoint
            const response = await fetch(
                "http://127.0.0.1:8000/signup",
                {
                    method: "POST",

                    headers: {
                        "Content-Type": "application/json"
                    },

                    body: jsonData
                }
            );


            // Convert FastAPI JSON response
            const result = await response.json();


            // Check HTTP response
            if (response.ok) {

                successMessage.textContent =
                    result.message || "Signup successful.";

                console.log("Backend response:");
                console.log(result);

            } else {

                console.error("Backend error:");
                console.error(result);

                successMessage.textContent =
                    "Signup failed. Please try again.";
            }


        } catch (error) {

            console.error("Error connecting to backend:");
            console.error(error);

            successMessage.textContent =
                "Unable to connect to the server.";
        }
    }
});

