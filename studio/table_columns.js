// Keep widths on <col> elements so row refreshes and language changes preserve them.
export function setupTableColumns() {
  document.querySelectorAll("[data-resizable-table]").forEach((table) => {
    const headers = [...table.tHead.rows[0].cells];
    const columns = [...table.querySelectorAll("colgroup > col")];
    const defaults = columns.map((column) => parseFloat(column.style.width));
    const minimums = defaults.map((width) => Math.min(width, 64));
    const storageKey = `sda.studio.tableColumns.v1.${table.dataset.resizableTable}`;
    let widths = [...defaults];
    try {
      const stored = JSON.parse(localStorage.getItem(storageKey));
      if (Array.isArray(stored) && stored.length === columns.length) {
        widths = stored.map((width, index) => Number.isFinite(width)
          ? Math.max(minimums[index], Math.min(4000, width)) : defaults[index]);
      }
    } catch { /* Column resizing also works when browser storage is unavailable. */ }

    const handles = [];
    const apply = () => {
      columns.forEach((column, index) => {
        column.style.width = `${widths[index]}px`;
        handles[index]?.setAttribute("aria-valuenow", String(Math.round(widths[index])));
      });
      table.style.width = `${widths.reduce((sum, width) => sum + width, 0)}px`;
    };
    const save = () => {
      try { localStorage.setItem(storageKey, JSON.stringify(widths)); } catch { /* Optional preference. */ }
    };
    const measure = () => {
      // A wide viewport can stretch a table to fill its container. Start a drag
      // from those displayed widths so neighbouring columns do not jump.
      widths = headers.map((header, index) => header.getBoundingClientRect().width || widths[index]);
    };
    const resize = (index, width) => {
      widths[index] = Math.max(minimums[index], Math.min(4000, width));
      apply();
    };

    headers.forEach((header, index) => {
      // applyLocale replaces translated textContent. Translate a child span
      // instead so it never removes the resize handle or its listeners.
      if (header.dataset.i18n || !header.children.length) {
        const label = document.createElement("span");
        label.className = "table-header-label";
        label.textContent = header.textContent;
        if (header.dataset.i18n) {
          label.dataset.i18n = header.dataset.i18n;
          label.dataset.i18nTitle = header.dataset.i18nTitle || header.dataset.i18n;
          delete header.dataset.i18n;
        }
        header.replaceChildren(label);
      }
      const handle = document.createElement("span");
      handle.className = "column-resize-handle";
      handle.tabIndex = 0;
      handle.setAttribute("role", "separator");
      handle.setAttribute("aria-orientation", "vertical");
      handle.setAttribute("aria-valuemin", String(minimums[index]));
      handle.setAttribute("aria-valuemax", "4000");
      handle.dataset.i18nTitle = "table.resizeColumn";
      handle.dataset.i18nAriaLabel = "table.resizeColumn";
      headers[index].append(handle);
      handles.push(handle);

      handle.addEventListener("click", (event) => event.stopPropagation());
      handle.addEventListener("dblclick", (event) => {
        event.preventDefault();
        event.stopPropagation();
        measure();
        resize(index, defaults[index]);
        save();
      });
      handle.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home"].includes(event.key)) return;
        event.preventDefault();
        event.stopPropagation();
        measure();
        const step = event.shiftKey ? 50 : 10;
        resize(index, event.key === "Home" ? defaults[index]
          : widths[index] + (event.key === "ArrowRight" ? step : -step));
        save();
      });
      handle.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || !event.isPrimary) return;
        event.preventDefault();
        event.stopPropagation();
        measure();
        const startX = event.clientX;
        const startWidth = widths[index];
        const scroller = table.parentElement;
        const startScroll = scroller.scrollLeft;
        handle.setPointerCapture(event.pointerId);
        handle.classList.add("dragging");
        document.documentElement.classList.add("resizing-table-column");
        const move = (nextEvent) => {
          if (nextEvent.pointerId !== event.pointerId) return;
          resize(index, startWidth + nextEvent.clientX - startX + scroller.scrollLeft - startScroll);
        };
        const end = (nextEvent) => {
          if (nextEvent.pointerId !== event.pointerId) return;
          handle.removeEventListener("pointermove", move);
          handle.removeEventListener("pointerup", end);
          handle.removeEventListener("pointercancel", end);
          handle.removeEventListener("lostpointercapture", end);
          if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
          handle.classList.remove("dragging");
          document.documentElement.classList.remove("resizing-table-column");
          save();
        };
        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", end);
        handle.addEventListener("pointercancel", end);
        handle.addEventListener("lostpointercapture", end);
      });
    });
    apply();
  });
}
