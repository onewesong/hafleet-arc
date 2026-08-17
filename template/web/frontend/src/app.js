const app = document.querySelector("#app");

fetch("/api/health")
  .then((response) => response.json())
  .then((health) => {
    app.dataset.backend = health.status;
  })
  .catch(() => {
    app.dataset.backend = "unavailable";
  });
