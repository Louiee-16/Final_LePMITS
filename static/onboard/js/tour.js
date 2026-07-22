/**
 * tour.js — LePMITS Shepherd.js Guided Tour
 */

function startTour(resume) {

  const tour = new Shepherd.Tour({
    useModalOverlay: true,
    defaultStepOptions: {
      cancelIcon: { enabled: true },
      scrollTo: { behavior: 'smooth', block: 'center' },
      modalOverlayOpeningRadius: 8,
      modalOverlayOpeningPadding: 6,
    },
  });

  tour.on('show', ({ step }) => localStorage.setItem('tourStep', step.id));
  tour.on('complete', () => {
    localStorage.removeItem('tourStep');
    localStorage.setItem('lepmits-tour-done', '1');
  });
  tour.on('cancel', () => localStorage.removeItem('tourStep'));

  // ── Button helpers ─────────────────────────────────────────────────────────
  const skip = { text: '✕ Skip Tour', action: () => tour.cancel(), classes: 'shepherd-btn-skip' };
  const back = { text: '← Back',      action: () => tour.back(),   classes: 'shepherd-btn-secondary' };
  const next = { text: 'Next →',      action: () => tour.next(),   classes: 'shepherd-btn-primary' };

  // ── Step 0 — Welcome (no attachment) ──────────────────────────────────────
  tour.addStep({
    id: 'welcome',
    title: '👋 Welcome to LePMITS',
    text: `
      <p>This is the <strong>Legislative Performance Monitoring and Information Tracking System</strong> for San Juan City.</p>
      <p style="margin-top:10px;">This quick tour walks you through the key parts of the system.</p>
    `,
    buttons: [skip, { text: 'Start Tour →', action: () => tour.next(), classes: 'shepherd-btn-primary' }],
  });

  // ── Step 1 — Sidebar navigation ────────────────────────────────────────────
  tour.addStep({
    id: 'sidebar-nav',
    title: '🗂 Sidebar Navigation',
    text: `
      <p>The <strong>sidebar</strong> is your main navigation panel. It adapts to your role — Secretariat, Councilor, Admin, or Staff.</p>
      <p style="margin-top:10px;">Collapse it anytime with the arrow button at the top to get more screen space.</p>
    `,
    attachTo: { element: '#sidebar-nav', on: 'right' },
    buttons: [skip, back, next],
  });

  // ── Step 2 — Role section ──────────────────────────────────────────────────
  if (USER_ROLE === 'COUNCILOR') {
    tour.addStep({
      id: 'role-section',
      title: '✍️ Your Work Section',
      text: `
        <p>This is your personal workspace — <strong>Draft Measures</strong>, <strong>Filed Measures</strong>, and your Committee assignments are all here.</p>
      `,
      attachTo: { element: '#councilor-section', on: 'right' },
      buttons: [skip, back, next],
    });
  } else if (USER_ROLE === 'SECRETARIAT' || USER_ROLE === 'STAFF') {
    tour.addStep({
      id: 'role-section',
      title: '📊 Tracking Stages',
      text: `
        <p>This section gives you access to all document tracking tools — from <strong>First Reading</strong> through <strong>Approval</strong>.</p>
        <p style="margin-top:10px;">Documents flow through each stage as they progress through the legislative process.</p>
      `,
      attachTo: { element: '#tracking-stages', on: 'right' },
      buttons: [skip, back, next],
    });
  }

  // ── Step 3 — Navigate to target page ──────────────────────────────────────
  tour.addStep({
    id: 'go-to-docs',
    title: USER_ROLE === 'COUNCILOR' ? '📝 Let\'s Draft a Measure' : '📄 Let\'s Look at Incoming Documents',
    text: USER_ROLE === 'COUNCILOR'
      ? `<p>As a Councilor, you can <strong>draft and file measures</strong> directly from the system.</p>
         <p style="margin-top:10px;">Click below and the tour will resume there.</p>`
      : `<p>All documents filed by Councilors land in <strong>Incoming Documents</strong> first.</p>
         <p style="margin-top:10px;">Click below and the tour will automatically resume there.</p>`,
    buttons: [
      skip,
      back,
      {
        text: USER_ROLE === 'COUNCILOR' ? 'Go to Draft →' : 'Go to Incoming Docs →',
        classes: 'shepherd-btn-primary',
        action: () => {
          localStorage.setItem('tourStep', 'docs-table');
          window.location.href = USER_ROLE === 'COUNCILOR' ? '/create/' : '/incoming_docs/';
        },
      },
    ],
  });

  // ── Step 4 — Documents table ───────────────────────────────────────────────
  tour.addStep({
    id: 'docs-table',
    title: USER_ROLE === 'COUNCILOR' ? '📝 Draft a New Measure' : '📋 Incoming Documents',
    text: USER_ROLE === 'COUNCILOR'
      ? `<p>Use this form to write and submit a new <strong>Ordinance or Resolution</strong>. Fill in the title, body, then click <strong>Save Draft</strong> or <strong>File Draft</strong>.</p>`
      : `<p>All received documents appear here. The Secretariat reviews them and moves them to <strong>First Reading</strong> once presented at a session.</p>`,
    attachTo: {
      element: USER_ROLE === 'COUNCILOR' ? '#draftForm' : '#tour-incoming-table',
      on: 'top',
    },
    buttons: [skip, back, next],
  });

  // ── Step 5 — Navigate to First Reading ────────────────────────────────────
  tour.addStep({
    id: 'go-to-first-reading',
    title: '📖 Let\'s See First Reading',
    text: `
      <p>Measures begin their legislative journey at <strong>First Reading</strong>, where they are formally presented and referred to a committee.</p>
      <p style="margin-top:10px;">Click below to continue the tour there.</p>
    `,
    buttons: [
      skip,
      back,
      {
        text: 'Go to First Reading →',
        classes: 'shepherd-btn-primary',
        action: () => {
          localStorage.setItem('tourStep', 'first-reading-table');
          window.location.href = '/view/first_reading/';
        },
      },
    ],
  });

  // ── Step 6 — First Reading table ──────────────────────────────────────────
  tour.addStep({
    id: 'first-reading-table',
    title: '📖 First Reading',
    text: `
      <p>Measures enter the legislative process here. The Secretariat logs them after they are read at a session.</p>
      <p style="margin-top:10px;">Once referred to a committee they move to the <strong>Committee Level</strong> for deliberation.</p>
    `,
    attachTo: { element: '#tour-first-reading-table', on: 'top' },
    buttons: [skip, back, next],
  });

  // ── Step 7 — User card ────────────────────────────────────────────────────
  tour.addStep({
    id: 'user-card',
    title: '👤 Your Profile',
    text: `
      <p>Your <strong>username</strong> and <strong>role</strong> are always visible here at the bottom of the sidebar.</p>
      <p style="margin-top:10px;">Use <strong>Log Out</strong> when you're done. Your session also auto-expires after inactivity for security.</p>
    `,
    attachTo: { element: '#userCard', on: 'right' },
    buttons: [
      skip,
      back,
      { text: '✅ Finish Tour', action: () => tour.complete(), classes: 'shepherd-btn-primary' },
    ],
  });

  // ── Start / resume ─────────────────────────────────────────────────────────
  const savedStepId = resume ? localStorage.getItem('tourStep') : null;
  tour.start();
  if (savedStepId && tour.getById(savedStepId)) {
    tour.show(savedStepId);
  }
}

// Resume tour on every page load if in progress
document.addEventListener('DOMContentLoaded', () => {
  if (localStorage.getItem('tourStep') !== null) {
    setTimeout(() => startTour(true), 600);
  }
});
