// The in-page "add to wishlist?" card. Shared by content.js and preview.html,
// so the preview always shows exactly what users see. Edit the look and wording here.
(function (root) {
  const MARKUP = `
      <style>
        :host { all: initial; }
        .card { width: 300px; box-sizing: border-box; padding: 16px; border-radius: 14px; background: #fff;
          color: #1f1f1f; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          box-shadow: 0 10px 30px rgba(0,0,0,.18); border: 1px solid #e6e6e6; }
        .top { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
        .title { font-weight: 600; margin: 0 0 6px; }
        .close { all: unset; cursor: pointer; color: #777; font-size: 18px; line-height: 1; padding: 2px 4px; }
        p { margin: 0 0 12px; color: #444; }
        .row { display: flex; gap: 8px; margin-bottom: 12px; }
        label { flex: 1; font-size: 12px; color: #666; }
        input { width: 100%; box-sizing: border-box; margin-top: 4px; padding: 7px 8px; border: 1px solid #ccc;
          border-radius: 8px; font: inherit; color: #1f1f1f; background: #fff; }
        .actions { display: flex; gap: 8px; }
        button.main, button.ghost { flex: 1; padding: 9px 10px; border-radius: 9px; font: inherit; font-weight: 600; cursor: pointer; }
        button.main { background: #1f1f1f; color: #fff; border: 1px solid #1f1f1f; }
        button.ghost { background: #fff; color: #1f1f1f; border: 1px solid #ccc; }
        .done { color: #2f6b3a; margin: 0; }
      </style>
      <div class="card" role="dialog" aria-label="Add to wishlist">
        <div class="top">
          <div class="title">Still thinking about this one?</div>
          <button class="close" aria-label="Close">&times;</button>
        </div>
        <p></p>
        <div class="row">
          <label>Size<input name="size" maxlength="20" placeholder="e.g. 6 or M"></label>
          <label>Color<input name="color" maxlength="40" placeholder="optional"></label>
        </div>
        <div class="actions">
          <button class="ghost">No thanks</button>
          <button class="main">Add to wishlist</button>
        </div>
      </div>`;

  function message(prompt) {
    const times = prompt.view_count ? `${prompt.view_count} times` : "a few times";
    const days = prompt.window_days || 7;
    return (
      `You have looked at ${prompt.product_name || "this item"} ${times} in the last ${days} days. ` +
      "Want me to watch it? I will email you when your size is back or the price drops."
    );
  }

  function render(shadow, prompt) {
    shadow.innerHTML = MARKUP;
    shadow.querySelector("p").textContent = message(prompt);
  }

  function markSaved(shadow) {
    shadow.querySelector(".card").innerHTML = '<p class="done">Saved. I will keep an eye on it for you.</p>';
  }

  root.ShoppingCard = { render, markSaved, message };
})(globalThis);
