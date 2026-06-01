// Reusable type-to-search multi-select with chips and optional "create new".
//
// Used by the admin user editor (assign groups, allowCreate=true) and the
// Groups tab's "Add member" picker (pick existing users, allowCreate=false).
//
// Usage:
//   const cs = createChipSelect({
//       selected: ["eng"],                 // initial chip values
//       options: () => ["eng","sales",…],  // current candidate values (fn or array)
//       allowCreate: true,                 // show "Create new "x"" when no exact match
//       placeholder: "type to add…",
//       onChange: (values) => { … },       // called with the full value array on every change
//   });
//   container.append(cs.el);
//   cs.getValues();   // → string[]
//   cs.setValues([…]) // replace programmatically (re-renders, no onChange)

export function createChipSelect(opts) {
    const {
        selected = [],
        options = [],
        allowCreate = false,
        placeholder = "type to add…",
        onChange = () => {},
    } = opts;

    let values = [...new Set(selected)];
    const optionsOf = typeof options === "function" ? options : () => options;

    const el = document.createElement("div");
    el.className = "chip-select";

    const chipWrap = document.createElement("div");
    chipWrap.className = "chip-select-chips";

    const input = document.createElement("input");
    input.type = "text";
    input.className = "chip-select-input";
    input.placeholder = placeholder;
    input.autocomplete = "off";

    const menu = document.createElement("div");
    menu.className = "chip-select-menu";
    menu.hidden = true;

    chipWrap.append(input);
    el.append(chipWrap, menu);

    function emit() { onChange([...values]); }

    function addValue(v) {
        const clean = v.trim();
        if (!clean || values.includes(clean)) { input.value = ""; renderMenu(); return; }
        values.push(clean);
        input.value = "";
        renderChips();
        renderMenu();
        emit();
    }

    function removeValue(v) {
        values = values.filter((x) => x !== v);
        renderChips();
        renderMenu();
        emit();
    }

    function renderChips() {
        // Rebuild chips but keep the input element in place.
        for (const c of [...chipWrap.querySelectorAll(".chip")]) c.remove();
        for (const v of values) {
            const chip = document.createElement("span");
            chip.className = "chip";
            const label = document.createElement("span");
            label.textContent = v;
            const x = document.createElement("button");
            x.type = "button";
            x.className = "chip-x";
            x.textContent = "✕";
            x.title = "Remove";
            x.addEventListener("click", (e) => { e.stopPropagation(); removeValue(v); });
            chip.append(label, x);
            chipWrap.insertBefore(chip, input);
        }
    }

    function renderMenu() {
        const q = input.value.trim().toLowerCase();
        const candidates = optionsOf()
            .filter((o) => !values.includes(o))
            .filter((o) => !q || o.toLowerCase().includes(q));

        menu.textContent = "";
        const exact = q && optionsOf().some((o) => o.toLowerCase() === q);
        let shown = 0;
        for (const o of candidates.slice(0, 20)) {
            const item = document.createElement("div");
            item.className = "chip-select-item";
            item.textContent = o;
            item.addEventListener("mousedown", (e) => { e.preventDefault(); addValue(o); });
            menu.append(item);
            shown++;
        }
        if (allowCreate && q && !exact) {
            const create = document.createElement("div");
            create.className = "chip-select-item chip-select-create";
            create.textContent = `＋ Create "${input.value.trim()}"`;
            create.addEventListener("mousedown", (e) => { e.preventDefault(); addValue(input.value); });
            menu.append(create);
            shown++;
        }
        menu.hidden = shown === 0;
    }

    input.addEventListener("input", renderMenu);
    input.addEventListener("focus", renderMenu);
    input.addEventListener("blur", () => { setTimeout(() => { menu.hidden = true; }, 120); });
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            // Enter commits the first menu item if present, else the raw text
            // (when allowCreate). Mirrors the click affordances above.
            const first = menu.querySelector(".chip-select-item:not(.chip-select-create)");
            if (first && !first.classList.contains("chip-select-create")) addValue(first.textContent);
            else if (allowCreate) addValue(input.value);
        } else if (e.key === "Backspace" && !input.value && values.length) {
            removeValue(values[values.length - 1]);
        }
    });

    renderChips();

    return {
        el,
        getValues: () => [...values],
        setValues: (next) => { values = [...new Set(next)]; renderChips(); renderMenu(); },
    };
}
