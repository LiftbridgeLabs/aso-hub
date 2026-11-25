// A chosen key file is read into its text box, so it's sent like pasted text.
for (const input of document.querySelectorAll("input[type=file][data-into]")) {
  input.addEventListener("change", async () => {
    const file = input.files[0];
    if (file && file.size < 50000) document.getElementById(input.dataset.into).value = await file.text();
    else if (file) alert("That file is too big to be a key.");
  });
}
