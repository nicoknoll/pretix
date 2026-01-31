document.querySelectorAll(".ticketbourse-position-checkbox").forEach(element => {
    let parent = document.getElementById(element.dataset.addonto);
    if (element.dataset.addonto && parent) {
        element.addEventListener("input", event => {
            if (!element.checked && parent.checked)
                parent.checked = false;
        })
    } else if (!element.dataset.addonto) {
        let children = document.querySelectorAll(".ticketbourse-position-checkbox[data-addonto='" + element.id + "']");
        element.addEventListener("input", event => {
            children.forEach(child => {
                if (element.checked && !child.checked)
                    child.checked = true;
            })

        })
    }
})
