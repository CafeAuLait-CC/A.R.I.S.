"use strict";
(() => {
  window.arisCalendarRange = function (timezone, preset) {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
    }).formatToParts(new Date());
    const date = Object.fromEntries(parts.map(part => [part.type, part.value]));
    const end = new Date(`${date.year}-${date.month}-${date.day}T12:00:00Z`);
    const start = new Date(end);
    if (preset === 'seven') {
      start.setUTCDate(start.getUTCDate() - 7);
      end.setUTCDate(end.getUTCDate() - 1);
    } else {
      start.setUTCDate(start.getUTCDate() - (start.getUTCDay() + 6) % 7);
    }
    return [start.toISOString().slice(0, 10), end.toISOString().slice(0, 10)];
  };
  const $ = id => document.getElementById(id);
  const top = $('workbench-top');
  if (!top) return;

  const rail = $('workbench-rail');
  const menu = $('workbench-menu');
  const account = $('account');
  const links = document.querySelectorAll('#workbench-nav [data-view]');

  function showMenu(open) {
    menu.setAttribute('aria-expanded', String(open));
    rail.classList.toggle('is-open', open);
  }

  menu.addEventListener('click', () => showMenu(!rail.classList.contains('is-open')));
  $('workbench-nav').addEventListener('click', event => {
    if (event.target.closest('a') && rail.classList.contains('is-open')) {
      showMenu(false);
      menu.focus();
    }
  });
  account.addEventListener('click', event => {
    if (event.target.closest('a')) account.open = false;
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    if (rail.classList.contains('is-open')) {
      showMenu(false);
      menu.focus();
    } else if (account.open) {
      account.open = false;
      account.querySelector('summary').focus();
    }
  });

  window.arisWorkbench = {
    session(actor) {
      showMenu(false);
      account.open = false;
      rail.hidden = top.hidden = !actor;
      $('workbench-admin').hidden = actor?.role !== 'admin';
      $('actor-name').textContent = actor?.display_name || '';
      $('actor-role').textContent = actor ? (actor.role === 'admin' ? 'Admin' : 'Member') : '';
      const names = (actor?.display_name || 'A').trim().split(/\s+/).slice(0, 2);
      $('actor-avatar').textContent = names.map(name => Array.from(name)[0]).join('').toUpperCase();
      $('workbench-timezone').textContent = actor?.timezone === 'America/Vancouver' ? '温哥华时间' : actor?.timezone || '';
      if (!actor) this.usage(null);
    },
    usage(access) {
      const reports = $('reports-nav');
      reports.hidden = !(access?.saved_snapshots_available || access?.weekly_reports_available);
      reports.hash = access?.saved_snapshots_available ? 'saved' : 'weekly';
      $('archive-nav').hidden = !access?.activity_preview_nodes.length;
    },
    view(view) {
      for (const link of links) {
        if (link.dataset.view === view) {
          link.setAttribute('aria-current', 'page');
          $('workbench-group').textContent = link.dataset.group;
          $('workbench-title').textContent = link.textContent.trim();
        } else {
          link.removeAttribute('aria-current');
        }
      }
    },
  };
})();
