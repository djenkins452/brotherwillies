// Help modal toggle
function toggleHelp() {
    var modal = document.getElementById('help-modal');
    var sheet = document.getElementById('help-sheet');
    if (modal.style.display === 'none' || modal.style.display === '') {
        // Reset drag position on open
        if (sheet) {
            sheet.style.position = '';
            sheet.style.left = '';
            sheet.style.top = '';
            sheet.style.margin = '';
        }
        modal.style.display = 'flex';
        document.body.style.overflow = 'hidden';
    } else {
        modal.style.display = 'none';
        document.body.style.overflow = '';
    }
}

function closeHelpOutside(event) {
    if (event.target.classList.contains('help-overlay')) {
        toggleHelp();
    }
}

// Profile dropdown toggle
function toggleProfileDropdown() {
    var dropdown = document.getElementById('profile-dropdown');
    if (dropdown) {
        dropdown.classList.toggle('open');
    }
}

// Close dropdowns on outside click
document.addEventListener('click', function(e) {
    var dropdown = document.getElementById('profile-dropdown');
    if (dropdown && dropdown.classList.contains('open')) {
        var wrap = dropdown.closest('.profile-dropdown-wrap');
        if (!wrap.contains(e.target)) {
            dropdown.classList.remove('open');
        }
    }
});

// Close help on Escape, also close profile dropdown
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        var modal = document.getElementById('help-modal');
        if (modal && modal.style.display === 'flex') {
            toggleHelp();
        }
        var dropdown = document.getElementById('profile-dropdown');
        if (dropdown) {
            dropdown.classList.remove('open');
        }
    }
});

// Slider value display
document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('input[type="range"]').forEach(function(slider) {
        var output = document.getElementById(slider.id + '-value');
        if (output) {
            output.textContent = parseFloat(slider.value).toFixed(2);
            slider.addEventListener('input', function() {
                output.textContent = parseFloat(this.value).toFixed(2);
            });
        }
    });
});

// ── Value Board Accordion ──────────────────────────────────────────────

function getVBSectionState() {
    try {
        var stored = localStorage.getItem('vb_sections');
        return stored ? JSON.parse(stored) : {};
    } catch (e) {
        return {};
    }
}

function saveVBSectionState(state) {
    try {
        localStorage.setItem('vb_sections', JSON.stringify(state));
    } catch (e) {}
}

function toggleVBSection(key) {
    var section = document.querySelector('[data-section-key="' + key + '"]');
    if (!section) return;

    var isOpen = section.classList.contains('open');
    section.classList.toggle('open');

    // Save state
    var state = getVBSectionState();
    state[key] = !isOpen;
    saveVBSectionState(state);
}

function expandAllVB() {
    var state = {};
    document.querySelectorAll('.vb-section').forEach(function(section) {
        section.classList.add('open');
        state[section.dataset.sectionKey] = true;
    });
    saveVBSectionState(state);
}

function collapseAllVB() {
    var state = {};
    document.querySelectorAll('.vb-section').forEach(function(section) {
        section.classList.remove('open');
        state[section.dataset.sectionKey] = false;
    });
    saveVBSectionState(state);
}

// Help modal drag (desktop only)
(function() {
    var handle = document.getElementById('help-drag-handle');
    var sheet = document.getElementById('help-sheet');
    if (!handle || !sheet) return;

    var isDragging = false;
    var startX, startY, origX, origY;

    handle.addEventListener('mousedown', function(e) {
        if (window.innerWidth <= 768) return;
        if (e.target.closest('.help-close')) return;
        isDragging = true;
        sheet.classList.add('is-dragging');
        var rect = sheet.getBoundingClientRect();
        origX = rect.left;
        origY = rect.top;
        startX = e.clientX;
        startY = e.clientY;
        // Switch from flex-centered to absolute positioning
        sheet.style.position = 'fixed';
        sheet.style.left = origX + 'px';
        sheet.style.top = origY + 'px';
        sheet.style.margin = '0';
        e.preventDefault();
    });

    document.addEventListener('mousemove', function(e) {
        if (!isDragging) return;
        var dx = e.clientX - startX;
        var dy = e.clientY - startY;
        sheet.style.left = (origX + dx) + 'px';
        sheet.style.top = (origY + dy) + 'px';
    });

    document.addEventListener('mouseup', function() {
        if (!isDragging) return;
        isDragging = false;
        sheet.classList.remove('is-dragging');
    });
})();

// Initialize accordion sections on page load — always use server defaults
document.addEventListener('DOMContentLoaded', function() {
    // Clear saved state so server-side smart defaults always apply
    saveVBSectionState({});
    document.querySelectorAll('.vb-section').forEach(function(section) {
        var defaultOpen = section.dataset.defaultOpen === 'true';
        if (defaultOpen) {
            section.classList.add('open');
        } else {
            section.classList.remove('open');
        }
    });
});
