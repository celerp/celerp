from fasthtml.common import Script, Div
def t(x): return x
def star_supporter_card() -> FT:
    """The GitHub-star ask: a gold-bordered, dismissable card. The COPY (header + body
    + tooltip) is the relay's single source of truth, hydrated from /stars/cta; the card
    shows only in a non-neutral mode (relay reachable) and when not dismissed. Rendered
    on the dashboard (where setup lands)."""
    # Fetch the CTA copy + the user's badge together. If they've already claimed, show
    # the relay's thank-you copy ({badge} -> their label); otherwise show the ask. All
    # copy stays relay-sourced; the card is hidden in neutral (relay down) or dismissed.
    js = (
        # Deferred one tick: when htmx swaps this card in for the import card, the
        # import card is still in the page while the swap runs.
        "setTimeout(function(){"
        "if(!window.celerpStarFetch)return;"
        # The ask waits for 10 days of use and for the import card to be gone.
        "if(!window.celerpUseDays||window.celerpUseDays(false)<10"
        "||document.getElementById('getting-started-card'))return;"
        "Promise.all(["
        "window.celerpStarFetch('/stars/cta?medium=dashboard').catch(function(){return null}),"
        "window.celerpStarFetch('/stars/badge').catch(function(){return null})"
        "]).then(function(res){"
        "var d=res[0],bd=res[1];"
        "if(!d||d.dismissed||d.mode==='neutral')return;"
        "var card=document.getElementById('star-supporter-card');if(!card)return;"
        "var h=document.getElementById('star-card-headline');"
        "var b=document.getElementById('star-card-body');"
        "var b2=document.getElementById('star-card-body-2');"
        "var actions=document.getElementById('star-card-actions');"
        "var wall=document.getElementById('star-card-wall');"
        "var link=document.getElementById('star-card-star');"
        "if(link&&d.url)link.href=d.url;"
        "if(bd&&bd.badge){"
        "if(h)h.textContent=d.thanks_headline||card.dataset.fallbackThanksHeadline;"
        "if(b)b.textContent=(d.thanks_body||'').replace('{badge}',bd.badge.label);"
        "if(b2)b2.style.display='none';"
        "if(actions)actions.style.display='none';"
        "if(wall&&bd.wall_url){wall.href=bd.wall_url;wall.style.display='inline-block';}"
        "}else{"
        "if(h)h.textContent=d.headline||card.dataset.fallbackHeadline;"
        "var parts=(d.body||'').split('\\n\\n');"
        "if(b)b.textContent=parts[0]||'';"
        "if(b2)b2.textContent=parts.slice(1).join('\\n\\n');"
        "if(wall)wall.style.display='none';"
        "}"
        "card.style.display='';"
        "}).catch(function(){});"
        "var dz=document.getElementById('star-card-dismiss');"
        "if(dz)dz.addEventListener('click',function(){"
        "fetch('/stars/dismiss',{method:'POST'}).then(function(){"
        "var c=document.getElementById('star-supporter-card');if(c)c.style.display='none';});});"
        "},0);"
    )
    gold = "color:#d4af37"
    # Static fallbacks for when the relay does not supply its own copy (R2): translated
    # here in Python and handed to the client via data-* attributes the script above
    # reads, never spliced into the JS source directly.
    fallback_headline = t("shell.star_on_github")
    fallback_thanks_headline = t("shell.thank_you_support")
    return Div(
        Div(
            # Dismiss = X in the top-right corner.
            Button("×", id="star-card-dismiss", type="button", title=t("settings.dismiss"),
                   aria_label=t("settings.dismiss"),
                   style="position:absolute;top:10px;right:14px;background:none;border:none;"
                         "font-size:24px;line-height:1;cursor:pointer;color:#999;padding:0"),
            # Header IS the link to the repo (gold star + the relay-hydrated ask).
            H3(
                A(Span("★ ", style=gold), Span("", id="star-card-headline"),
                  id="star-card-star", href="#", target="_blank", rel="noopener",
                  style="text-decoration:underline;color:inherit;cursor:pointer"),
                style="margin:0 0 14px",
            ),
            P("", id="star-card-body", style="white-space:pre-line;margin:0"),
            # Smaller sub-paragraph for the founding-badge promise.
            P("", id="star-card-body-2", style="white-space:pre-line;margin:12px 0 0;font-size:13px;color:#555"),
            # Claiming a badge lists the founder on the public wall (that is what the
            # badge is). Hidden once claimed; the wall link below takes its place.
            Div(
                A(t("shell.claim_your_badge"), id="star-card-claim", href="/stars/claim", cls="btn btn--primary"),
                P(t("shell.claim_badge_note"), id="star-card-claim-note",
                  style="margin:8px 0 0;font-size:13px;color:#555"),
                id="star-card-actions",
                style="margin-top:22px",
            ),
            # Shown only once claimed: a quiet link to the public founders wall.
            A(Span("★ ", style=gold), t("shell.see_founders_wall"),
              id="star-card-wall", href="#", target="_blank", rel="noopener",
              style="display:none;margin-top:18px;font-size:13px;text-decoration:underline;color:inherit"),
            id="star-supporter-card",
            data_fallback_headline=fallback_headline,
            data_fallback_thanks_headline=fallback_thanks_headline,
            style="display:none;position:relative;margin:16px 0;padding:28px 32px;"
                  "border:1px solid #d4af37;border-radius:10px;text-align:center",
        ),
        Script(js),
    )


_STICKY_HEADER_JS = """
(function(){
  // Freeze the header row of the excel-style list tables while the table body is on screen.
  // Opt-in by class: only table.data-table.sticky-head is touched. The header cannot both live
  // inside the horizontal-scroll wrap and stick to the window via pure CSS (the overflow-x wrap
  // clips position:sticky), so this pins the LIVE thead with position:fixed (from CSS) and keeps
  // the whole table's column geometry aligned. See app.css .data-table.sticky-head.hdr-pinned.
  var SEL = 'table.data-table.sticky-head';
  var mqPrint = window.matchMedia ? window.matchMedia('print') : null;
  var mo = null;
  // Set the moment print is requested and cleared when it ends. beforeprint fires before some
  // browsers flip the print media query, and a mutation arriving in that gap would otherwise
  // re-pin the header we just cleared; this flag keeps the controller a no-op through print.
  var printing = false;

  function isPinned(t){ return t.classList.contains('hdr-pinned'); }

  // The phantom top-scrollbar (.table-top-scroll) is a sibling of the table's .table-scroll-wrap,
  // both under #data-table-wrap, and exists only when the table has enough rows to warrant it
  // (ui/components/table.py). When the header freezes, the phantom must freeze stacked directly
  // above it so a user scrolled mid-table can still scroll left/right; it is null when absent.
  function wrapOf(t){ return t.closest('.table-scroll-wrap'); }
  function phantomOf(t){
    var w = wrapOf(t); var c = w && w.parentElement;
    return c ? c.querySelector(':scope > .table-top-scroll') : null;
  }

  // Every DOM write the controller makes (colgroup, spacer row, thead styles, the pinned class)
  // is itself a mutation. MutationObserver callbacks are async microtasks, so a boolean "busy"
  // guard is already reset by the time the callback runs and cannot suppress them - the observer
  // would re-fire on its own writes forever. Disconnect it for the duration of every write pass
  // instead; genuine external mutations (rows added, an inline edit) still fire once we reconnect.
  function pauseObserver(fn){
    if(mo){ mo.disconnect(); }
    try { fn(); } finally { observe(); }
  }

  function reposition(t){
    var thead = t.tHead; if(!thead) return;
    var rect = t.getBoundingClientRect();
    // rect.left already reflects the wrap's horizontal scroll, so it is the body's content left.
    thead.style.left = rect.left + 'px';
    thead.style.width = rect.width + 'px';
    // The header is position:fixed, so the wrap's overflow does not clip it: scrolled sideways it
    // would paint over the sidebar on the left and past the table on the right. Clip it to the
    // wrap's visible box, the same geometry the phantom scrollbar below already tracks. clip-path
    // clips painting and hit-testing but leaves getBoundingClientRect intact, so columns stay aligned.
    var w = wrapOf(t);
    if(w){
      var wr = w.getBoundingClientRect();
      var clipL = Math.max(0, wr.left - rect.left);
      var clipR = Math.max(0, (rect.left + rect.width) - (wr.left + w.clientWidth));
      thead.style.clipPath = 'inset(0 ' + clipR + 'px 0 ' + clipL + 'px)';
      // Keep the frozen phantom aligned with the on-screen wrap so its track spans the visible body.
      // Its scrollLeft stays synced to the wrap by the existing bidirectional listener, unaffected
      // by fixing position (that sync is scrollLeft-based, not layout-based).
      var ph = phantomOf(t);
      if(ph && ph.classList.contains('top-scroll-pinned')){
        ph.style.left = wr.left + 'px';
        ph.style.width = w.clientWidth + 'px';
      }
    } else {
      thead.style.clipPath = '';
    }
  }

  // Remove every colgroup/spacer the controller has ever added to this table. querySelectorAll,
  // not querySelector: a re-render can leave the artifacts in place while stripping the pinned
  // class, so pin/unpin must clear ALL of them or a duplicate survives every later cycle.
  function clearPinArtifacts(t){
    var cgs = t.querySelectorAll(':scope > colgroup.hdr-cg');
    for(var i=0;i<cgs.length;i++) cgs[i].remove();
    var tb = t.tBodies[0];
    if(tb){ var sps = tb.querySelectorAll(':scope > tr.hdr-spacer'); for(var j=0;j<sps.length;j++) sps[j].remove(); }
    // Revert the frozen phantom scrollbar and drop every top-scroll spacer this table ever added.
    var ph = phantomOf(t);
    if(ph){ ph.classList.remove('top-scroll-pinned'); ph.style.left = ''; ph.style.width = ''; }
    t.classList.remove('has-top-phantom');
    var wrap = wrapOf(t); var cont = wrap && wrap.parentElement;
    if(cont){ var tss = cont.querySelectorAll(':scope > .top-scroll-spacer'); for(var k=0;k<tss.length;k++) tss[k].remove(); }
  }

  function pin(t){
    var thead = t.tHead; if(!thead || !thead.rows.length) return;
    // Idempotent: drop any stale artifacts before recapturing, so re-pinning never duplicates them.
    clearPinArtifacts(t);
    // Visible cells only: the column manager hides a column by display:none on its th and tds,
    // and hidden cells generate no boxes. colgroup cols map to RENDERED boxes positionally, so a
    // col emitted for a hidden column would land on the next visible column and shift every
    // column after it under the wrong header.
    var allCells = thead.rows[0].cells, cells = [], ci;
    for(ci=0;ci<allCells.length;ci++){ if(getComputedStyle(allCells[ci]).display !== 'none') cells.push(allCells[ci]); }
    if(!cells.length) return;
    var widths = [], i;
    for(i=0;i<cells.length;i++) widths.push(cells[i].getBoundingClientRect().width);
    var theadH = thead.getBoundingClientRect().height;
    var tableW = t.getBoundingClientRect().width;
    // Lock the whole table's columns so the body does not re-resolve once the thead leaves flow.
    var cg = document.createElement('colgroup'); cg.className = 'hdr-cg';
    for(i=0;i<widths.length;i++){ var col = document.createElement('col'); col.style.width = widths[i] + 'px'; cg.appendChild(col); }
    t.insertBefore(cg, t.firstChild);
    t.style.tableLayout = 'fixed';
    t.style.width = tableW + 'px';
    // Spacer row keeps the body from jumping up by the header's height while it is fixed.
    var tb = t.tBodies[0];
    if(tb){
      var sp = document.createElement('tr'); sp.className = 'hdr-spacer';
      var td = document.createElement('td'); td.colSpan = cells.length;
      td.style.padding = '0'; td.style.border = '0'; td.style.height = theadH + 'px';
      sp.appendChild(td); tb.insertBefore(sp, tb.firstChild);
    }
    // The fixed thead is its own table box so its cells lay out at the captured widths.
    thead.style.display = 'table';
    thead.style.tableLayout = 'fixed';
    for(i=0;i<cells.length;i++) cells[i].style.width = widths[i] + 'px';
    // Freeze the phantom top-scrollbar stacked directly above the header when one exists. The
    // has-top-phantom class shifts the fixed thead down by the phantom height (CSS) so they stack;
    // a spacer where the phantom sat keeps the content below from jumping up as it leaves flow.
    var ph = phantomOf(t);
    if(ph){
      t.classList.add('has-top-phantom');
      ph.classList.add('top-scroll-pinned');
      var cont = wrapOf(t).parentElement;
      var sp2 = document.createElement('div'); sp2.className = 'top-scroll-spacer';
      sp2.style.height = ph.getBoundingClientRect().height + 'px';
      cont.insertBefore(sp2, ph);
    }
    t.classList.add('hdr-pinned');
    reposition(t);
  }

  function unpin(t){
    t.classList.remove('hdr-pinned');
    var thead = t.tHead;
    if(thead){
      thead.style.display = ''; thead.style.tableLayout = ''; thead.style.width = ''; thead.style.left = ''; thead.style.clipPath = '';
      // Leave the cell widths in place: they carry the user's applied/resized column sizes, and a
      // re-pin recaptures from them. Blanking them here dropped custom widths on scroll-up and wiped
      // an in-progress resize before it could persist.
    }
    t.style.tableLayout = ''; t.style.width = '';
    clearPinArtifacts(t);
  }

  function unpinAll(){
    pauseObserver(function(){
      var tables = document.querySelectorAll(SEL);
      for(var i=0;i<tables.length;i++) if(isPinned(tables[i])) unpin(tables[i]);
    });
  }

  function evaluate(full){
    if(printing || (mqPrint && mqPrint.matches)){ unpinAll(); return; }
    // A column resize sets th widths pixel by pixel; leave the header alone until the drag ends,
    // or the per-mutation re-pin recaptures the pre-drag width every move and the drag never sticks.
    if(document.body && document.body.classList.contains('col-resizing')) return;
    pauseObserver(function(){
      var tables = document.querySelectorAll(SEL);
      for(var i=0;i<tables.length;i++){
        var t = tables[i];
        var thead = t.tHead; if(!thead){ continue; }
        // On a full re-evaluate (resize, re-render, reorder) drop stale geometry so widths recapture.
        if(full && isPinned(t)) unpin(t);
        var rect = t.getBoundingClientRect();
        var theadH = thead.getBoundingClientRect().height;
        var shouldPin = rect.top <= 0 && (rect.bottom - theadH) > 0;
        if(shouldPin){
          if(!isPinned(t)) pin(t); else reposition(t);
        } else if(isPinned(t)){
          unpin(t);
        }
      }
    });
  }

  function onScroll(){ evaluate(false); }
  function onFull(){ evaluate(true); }

  document.addEventListener('DOMContentLoaded', onFull);
  // Capture phase so whichever element scrolls vertically (window or an inner wrap) is caught,
  // and so the wrap's horizontal scroll repositions the frozen header in lockstep with the body.
  document.addEventListener('scroll', onScroll, true);
  window.addEventListener('resize', onFull);
  document.addEventListener('htmx:afterSwap', onFull);
  document.addEventListener('celerp:col-reorder', onFull);
  window.addEventListener('beforeprint', function(){ printing = true; unpinAll(); });
  window.addEventListener('afterprint', function(){ printing = false; onScroll(); });

  mo = new MutationObserver(onFull);
  // attributes:true catches a column-resize style write on a th; the controller's own writes
  // never re-fire it because pauseObserver disconnects the observer for the duration of each pass.
  function observe(){ if(mo && document.body) mo.observe(document.body, {childList:true, subtree:true, attributes:true}); }
  if(document.body) observe(); else document.addEventListener('DOMContentLoaded', observe);
})();

(function(){
  // Scroll cue: a data table's box or a tab strip that scrolls sideways shades the edge it can still
  // scroll toward (a box that does not scroll is never shaded) (app.css .scroll-more-left/right), so
  // hidden columns and tabs read as there.
  function update(box){
    var max = /auto|scroll/.test(getComputedStyle(box).overflowX) ? box.scrollWidth - box.clientWidth : 0;
    box.classList.toggle('scroll-more-right', max > 1 && box.scrollLeft < max - 1);
    box.classList.toggle('scroll-more-left', max > 1 && box.scrollLeft > 1);
    box.style.setProperty('--scroll-x', box.scrollLeft + 'px');
  }
  // The box changes width with the window; the table changes width with its rows. A swap that
  // takes either out of the page ends the watch on it.
  var sizes = new ResizeObserver(function(entries){
    entries.forEach(function(e){
      var box = e.target.matches('table') ? e.target.parentElement : e.target;
      if(!e.target.isConnected || !box){ sizes.unobserve(e.target); return; }
      update(box);
    });
  });
  function cue(box, inner){
    if(!box || box.dataset.scrollCue) return;
    box.dataset.scrollCue = '1';
    box.addEventListener('scroll', function(){ update(box); }, {passive: true});
    sizes.observe(box); if(inner) sizes.observe(inner);
    update(box);
  }
  function watch(){
    document.querySelectorAll('table.data-table').forEach(function(t){ cue(t.parentElement, t); });
    document.querySelectorAll('.category-tabs').forEach(function(strip){ cue(strip); });
  }
  document.addEventListener('DOMContentLoaded', watch);
  document.addEventListener('htmx:afterSwap', watch);
})();
"""


