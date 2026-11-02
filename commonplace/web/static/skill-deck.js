/* Text stays in server-escaped <pre> elements; never render imported Markdown as HTML. */
const deck = document.body.dataset.deck;
const base = `/api/skill-decks/${encodeURIComponent(deck)}`;
let receipt = null;

function requestError(error) {
  if (error.message === "unauthorized" || error.message === "signed-in workspace owner required") {
    return "Sign in as a workspace owner to continue.";
  }
  if (error.message === "deck not found") return "This deck is no longer available.";
  return "The request could not be completed. Please try again.";
}

async function post(action, value) {
  const response = await fetch(`${base}/${action}`, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed");
  return result;
}

post("preview", {}).then(result => {
  receipt = result.receipt;
  document.querySelector("#target").textContent = `Install into ${result.workspace}.`;
  document.querySelector("#confirm").disabled = false;
  document.querySelector("#add").disabled = false;
}).catch(error => {
  document.querySelector("#target").textContent = error.message === "unauthorized"
    ? "Sign in as a workspace owner to install." : requestError(error);
});

document.querySelector("#install").addEventListener("submit", async event => {
  event.preventDefault();
  const result = document.querySelector("#install-result");
  const button = document.querySelector("#add");
  if (!receipt || !document.querySelector("#confirm").checked) return;
  button.disabled = true;
  try {
    const installed = await post("install", { receipt, confirm: true });
    const count = installed.skills.length;
    result.textContent = `Installed ${count} ${count === 1 ? "skill" : "skills"} as quoted data.`;
    button.textContent = "Installed";
    document.querySelector("#confirm").disabled = true;
  } catch (error) {
    if (error.message === "preview this deck in this workspace before installing") {
      receipt = null;
      const confirm = document.querySelector("#confirm");
      confirm.checked = false;
      confirm.disabled = true;
      result.textContent = "This preview expired. Reload to preview again. ";
      const reload = document.createElement("a");
      reload.href = window.location.href;
      reload.textContent = "Reload preview";
      result.append(reload);
    } else {
      result.textContent = requestError(error);
      button.disabled = false;
    }
  }
});

document.querySelector("#report").addEventListener("submit", async event => {
  event.preventDefault();
  const result = document.querySelector("#report-result");
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  try {
    const report = await post("report", { reason: document.querySelector("#reason").value });
    result.textContent = `${report.notice} Reference: ${report.report_id}`;
  } catch (error) {
    result.textContent = error.message === "unauthorized" ? "Sign in to send a report." : requestError(error);
  } finally {
    button.disabled = false;
  }
});
