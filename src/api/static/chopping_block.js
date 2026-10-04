/* Chopping-block heads-up (item 228): every holding, visibility only.
   One card per holding, stacked, so a phone never scrolls sideways. */
async function loadChoppingBlock() {
  const body = document.querySelector("#panel-chopping-block [data-body]");
  try {
    const data = await fetchJSON("/chopping-block");
    body.replaceChildren();
    if (data.summary) body.appendChild(el("div", { className: "cb-summary", text: data.summary }));
    const cards = data.holdings.map((h) => {
      const below = h.standing === "below_bar";
      const tone = below ? "cb-below" : (h.direction === "closing_in" || !h.distance_known ? "cb-watch" : "cb-clear");
      const label = below ? "below the bar" : (h.direction === "closing_in" ? "closing in" : (h.distance_known ? "clears the bar" : "distance unknown"));
      return el("div", { className: `cb-card ${tone}` }, [
        el("div", { className: "cb-top" }, [
          el("strong", { text: h.symbol }),
          el("span", { className: "cb-tag", text: label }),
        ]),
        el("div", { className: "cb-distance", text: h.distance }),
        el("div", { className: "cb-headline", text: h.headline }),
        el("div", { className: "cb-reason dim", text: h.reason }),
      ]);
    });
    cards.forEach((c) => body.appendChild(c));
    body.appendChild(el("div", { className: "dim cb-note", text: data.note }));
    setPanelState("panel-chopping-block", "ok", "ok");
  } catch (err) {
    showMessage(body, `Could not load the chopping-block heads-up: ${err.message}`, true);
    setPanelState("panel-chopping-block", "error", "unreachable");
  }
}
