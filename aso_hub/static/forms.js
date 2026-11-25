// While a pull or digest runs, wait for it, then load the page again to show the result.
if (document.querySelector("[data-running]")) {
  const check = async () => {
    try {
      const response = await fetch("/settings/schedule/status", { cache: "no-store" });
      if (response.redirected || !(await response.json()).running) {
        location.assign("/settings/schedule?saved=Finished.+The+result+is+below.");
        return;
      }
    } catch { /* the hub didn't answer: ask again */ }
    setTimeout(check, 3000);
  };
  setTimeout(check, 3000);
}

// A chosen key file is read into its text box, so it's sent like pasted text.
for (const input of document.querySelectorAll("input[type=file][data-into]")) {
  input.addEventListener("change", async () => {
    const file = input.files[0];
    if (file && file.size < 50000) document.getElementById(input.dataset.into).value = await file.text();
    else if (file) alert("That file is too big to be a key.");
  });
}
