/**
 * tour.js — LePMITS Shepherd.js Guided Tour
 *
 * Branches by USER_ROLE (set in base.html) so each role only sees the parts
 * of the system it actually has. Not available to ADMIN — the "Take a Tour"
 * button itself is hidden for that role in base.html, since Admin has no
 * document workflow to walk through. COUNCILOR / SECRETARIAT / STAFF /
 * BARANGAY share the same document-lifecycle walkthrough with role-specific
 * detours where their workflows diverge (drafting vs. incoming vs. upload).
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

  const isSecretariat = USER_ROLE === 'SECRETARIAT' || USER_ROLE === 'STAFF';

  // ── Button helpers ─────────────────────────────────────────────────────────
  const skip = { text: '✕ Skip Tour', action: () => tour.cancel(), classes: 'shepherd-btn-skip' };
  const back = { text: '← Back',      action: () => tour.back(),   classes: 'shepherd-btn-secondary' };
  const next = { text: 'Next →',      action: () => tour.next(),   classes: 'shepherd-btn-primary' };
  const finish = { text: '✅ Finish Tour', action: () => tour.complete(), classes: 'shepherd-btn-primary' };

  // Navigates to `url`, remembering `resumeId` so the tour picks back up
  // there once the new page loads (see the DOMContentLoaded resume hook at
  // the bottom of this file).
  function goTo(resumeId, url) {
    return {
      classes: 'shepherd-btn-primary',
      action: () => {
        localStorage.setItem('tourStep', resumeId);
        window.location.href = url;
      },
    };
  }

  // ── Step 0 — Welcome (no attachment) ──────────────────────────────────────
  tour.addStep({
    id: 'welcome',
    title: '👋 Welcome to LePMITS',
    text: `
      <p>This is the <strong>Legislative Performance Monitoring and Information Tracking System</strong> for San Juan City — it manages a measure's full journey, from first draft to enacted law.</p>
      <p style="margin-top:10px;">This tour covers how documents move through that process, the AI-assisted tools built into the system, and what matters most for your role.</p>
    `,
    buttons: [skip, { text: 'Start Tour →', action: () => tour.next(), classes: 'shepherd-btn-primary' }],
  });

  // ── Step 1 — Sidebar navigation ────────────────────────────────────────────
  tour.addStep({
    id: 'sidebar-nav',
    title: '🗂 Sidebar Navigation',
    text: `
      <p>The <strong>sidebar</strong> is your main navigation panel. It adapts to your role — Secretariat, Councilor, or Barangay.</p>
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
  } else if (isSecretariat) {
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
  } else if (USER_ROLE === 'BARANGAY') {
    tour.addStep({
      id: 'role-section',
      title: '📋 Your Barangay Workspace',
      text: `
        <p>This is where your barangay's measures live. Upload an ordinance or resolution here — it's routed to the Secretariat, then to a committee for hearing.</p>
        <p style="margin-top:10px;">Once a hearing outcome is logged as <strong>Approved</strong>, your measure automatically receives its own official reference number.</p>
      `,
      attachTo: { element: '#barangay-section', on: 'right' },
      buttons: [skip, back, next],
    });
  }

  // ── Step 3 — Smart features (no attachment) ────────────────────────────────
  tour.addStep({
    id: 'smart-features',
    title: '✨ Smart Features Built Into This System',
    text: `
      <p><strong>Legal Basis Assistant</strong> — suggests supporting citations for a measure, but only from real, retrieved sources: actually enacted Republic Acts and genuinely pending bills in Congress. It never invents a citation from memory.</p>
      <p style="margin-top:10px;"><strong>Inline Similarity Check</strong> — runs quietly in the background while a measure is being drafted, flagging paragraphs that closely match existing legislation already on file. There's no button for it — it just works as you type.</p>
    `,
    buttons: [skip, back, next],
  });

  // ── Step 4 — Navigate to role's main workflow page ─────────────────────────
  {
    const dest = USER_ROLE === 'COUNCILOR'
      ? { title: '📝 Let\'s Draft a Measure', text: `<p>As a Councilor, you can <strong>draft and file measures</strong> directly from the system.</p><p style="margin-top:10px;">Click below and the tour will resume there.</p>`, label: 'Go to Draft →', url: '/create/', resumeId: 'docs-table' }
      : USER_ROLE === 'BARANGAY'
      ? { title: '📤 Let\'s Look at Your Measures', text: `<p>Your dashboard is also where you upload new measures for your barangay.</p><p style="margin-top:10px;">Click below and the tour will resume there.</p>`, label: 'Go to My Measures →', url: '/dashboard/', resumeId: 'docs-table' }
      : { title: '📄 Let\'s Look at Incoming Documents', text: `<p>All documents filed by Councilors land in <strong>Incoming Documents</strong> first.</p><p style="margin-top:10px;">Click below and the tour will automatically resume there.</p>`, label: 'Go to Incoming Docs →', url: '/incoming_docs/', resumeId: 'docs-table' };

    tour.addStep({
      id: 'go-to-docs',
      title: dest.title,
      text: dest.text,
      buttons: [skip, back, { text: dest.label, ...goTo(dest.resumeId, dest.url) }],
    });

    // ── Step 5 — Role's main workflow page ────────────────────────────────
    const table = USER_ROLE === 'COUNCILOR'
      ? { title: '📝 Draft a New Measure', text: `<p>Use this form to write and submit a new <strong>Ordinance or Resolution</strong>. Fill in the title, body, then click <strong>Save Draft</strong> or <strong>File Draft</strong>.</p>`, attach: '#casualdocsContainer' }
      : USER_ROLE === 'BARANGAY'
      ? { title: '📤 Upload a Measure', text: `<p>Use this button to submit a new ordinance or resolution from your barangay. Fill in the title and details, then upload the file.</p>`, attach: '#openModal' }
      : { title: '📋 Incoming Documents', text: `<p>All received documents appear here. The Secretariat reviews them and moves them to <strong>First Reading</strong> once presented at a session.</p>`, attach: '#tour-incoming-table' };

    tour.addStep({
      id: 'docs-table',
      title: table.title,
      text: table.text,
      attachTo: { element: table.attach, on: 'top' },
      buttons: [skip, back, next],
    });

    // ── Step 5b — Councilor-only: the Legal Basis button, hands-on ────────
    if (USER_ROLE === 'COUNCILOR') {
      tour.addStep({
        id: 'smart-drafting-tools',
        title: '⚖️ Legal Basis, One Click Away',
        text: `
          <p>Click <strong>Legal Basis</strong> anytime while drafting to get AI-suggested citations grounded in real enacted law and pending bills.</p>
          <p style="margin-top:10px;">Meanwhile, the editor is already quietly checking your paragraphs against existing legislation in the background — watch the bottom-left corner for a small indicator when it's running.</p>
        `,
        attachTo: { element: '#aiCheckBtn', on: 'bottom' },
        buttons: [skip, back, next],
      });
    }

    // ── Step 6 — Navigate to First Reading ───────────────────────────────
    tour.addStep({
      id: 'go-to-first-reading',
      title: '📖 Let\'s See First Reading',
      text: `
        <p>Measures begin their legislative journey at <strong>First Reading</strong>, where they are formally presented and referred to a committee.</p>
        <p style="margin-top:10px;">Click below to continue the tour there.</p>
      `,
      buttons: [skip, back, { text: 'Go to First Reading →', ...goTo('first-reading-table', '/view/first_reading/') }],
    });

    // ── Step 7 — First Reading table ─────────────────────────────────────
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

    // ── Step 8 — The rest of the legislative journey, summarized ────────
    tour.addStep({
      id: 'legislative-journey',
      title: '🔄 The Full Legislative Journey',
      text: `
        <p>From here, a measure moves through <strong>Committee Referral</strong> (hearings & deliberation) → <strong>Second Reading</strong> (amendments applied) → <strong>Third Reading</strong> (final vote, no further changes) → <strong>Approved</strong> or <strong>Disapproved</strong>.</p>
        <p style="margin-top:10px;">A measure returned from committee without a clear next step lands in <strong>Unfinished Business</strong>; barangay measures awaiting referral show up under <strong>Other Matters</strong>.</p>
      `,
      attachTo: { element: '#tracking-stages', on: 'right' },
      buttons: [skip, back, next],
    });
  }

  // ── Step 9 — User card (all roles) ─────────────────────────────────────────
  tour.addStep({
    id: 'user-card',
    title: '👤 Your Profile',
    text: `
      <p>Your <strong>username</strong> and <strong>role</strong> are always visible here at the bottom of the sidebar.</p>
      <p style="margin-top:10px;">Use <strong>Log Out</strong> when you're done. Your session also auto-expires after inactivity for security.</p>
      <p style="margin-top:10px;">You can restart this tour anytime from the <strong>Take a Tour</strong> button up top.</p>
    `,
    attachTo: { element: '#userCard', on: 'right' },
    buttons: [skip, back, finish],
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
