document.querySelectorAll(".copyable").forEach(element => {
    element.style.cursor = "pointer"
    element.addEventListener("click", event => {
        navigator.clipboard.writeText($(element).data("destination")).then(() => {
            // `event.target` is the <strong> element inside the span that is `element`
            event.target.title = "Copied!"
            $(event.target).tooltip("show")
            window.setTimeout(() => {
                $(event.target).tooltip("hide")
            }, 600)
        })
        return false
    })
})
