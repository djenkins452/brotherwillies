// Help modal toggle
function toggleHelp() {
    var modal = document.getElementById('help-modal');
    if (modal.style.display === 'none' || modal.style.display === '') {
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
