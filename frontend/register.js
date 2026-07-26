const form = document.querySelector("#register-form");
const input = document.querySelector("#username");
const status = document.querySelector("#status");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  status.textContent = "Checking your username...";
  try {
    const response = await fetch("/api/users/access", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: input.value.trim() }) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not register.");
    localStorage.setItem("biology_user", JSON.stringify(data));
    window.location.href = "/chat.html";
  } catch (error) {
    status.textContent = error.message;
  }
});
