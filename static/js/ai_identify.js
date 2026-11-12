(function() {
  var fileInput = document.getElementById('ai-photo');
  var fileName = document.getElementById('ai-file-name');
  var fileBtn = document.getElementById('ai-file-btn');
  var preview = document.getElementById('ai-preview');
  var previewImg = document.getElementById('ai-preview-img');
  var analyzeBtn = document.getElementById('ai-analyze-btn');
  var loading = document.getElementById('ai-loading');
  var errorBox = document.getElementById('ai-error');
  var results = document.getElementById('ai-results');

  if (!fileInput || !analyzeBtn) return;

  var selectedFile = null;

  fileBtn.addEventListener('click', function() { fileInput.click(); });
  fileInput.addEventListener('change', function() {
    if (fileInput.files && fileInput.files.length > 0) {
      selectedFile = fileInput.files[0];
      fileName.textContent = selectedFile.name;
      previewImg.src = URL.createObjectURL(selectedFile);
      preview.hidden = false;
      errorBox.hidden = true;
      errorBox.textContent = '';
      if (analyzeBtn.dataset.aiAvailable === 'true') {
        analyzeBtn.disabled = false;
      }
    }
  });

  function getAnalysisType() {
    var radios = document.querySelectorAll('input[name="analysis_type"]');
    for (var i = 0; i < radios.length; i++) {
      if (radios[i].checked) return radios[i].value;
    }
    return 'both';
  }

  function clearResults() {
    document.getElementById('ai-species-section').hidden = true;
    document.getElementById('ai-habitat-section').hidden = true;
  }

  function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/[&<>"']/g, function(c) {
      return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c];
    });
  }

  function renderSpecies(speciesList, summary) {
    var container = document.getElementById('ai-species-list');
    var section = document.getElementById('ai-species-section');
    var summaryEl = document.getElementById('ai-species-summary');

    if (!speciesList || speciesList.length === 0) {
      container.innerHTML = '<p class="ai-empty">No species could be confidently identified from this photo.</p>';
      summaryEl.textContent = summary || '';
    } else {
      var html = '';
      speciesList.forEach(function(s) {
        var conf = Math.round((s.confidence || 0) * 100);
        html += '<div class="ai-species-item">';
        html += '<div class="ai-species-head">';
        html += '<strong>' + escapeHtml(s.common_name || 'Unknown') + '</strong>';
        html += '<span class="ai-confidence" style="--conf:' + conf + '">' + conf + '%</span>';
        html += '</div>';
        if (s.scientific_name) html += '<em class="ai-scientific">' + escapeHtml(s.scientific_name) + '</em>';
        if (s.family) html += '<span class="ai-family">Family: ' + escapeHtml(s.family) + '</span>';
        if (s.notes) html += '<p class="ai-notes">' + escapeHtml(s.notes) + '</p>';
        html += '</div>';
      });
      container.innerHTML = html;
      summaryEl.textContent = summary || '';
    }
    section.hidden = false;
  }

  function renderHabitat(h) {
    var section = document.getElementById('ai-habitat-section');
    var body = document.getElementById('ai-habitat-body');
    if (!h) { section.hidden = true; return; }

    var conf = Math.round((h.confidence || 0) * 100);
    var rows = [
      ['Habitat Type', h.habitat_type],
      ['Vegetation', h.vegetation],
      ['Soil / Substrate', h.soil_or_substrate],
      ['Climate Indicators', h.climate_indicators],
    ];
    var html = '<div class="ai-habitat-head"><strong>' + escapeHtml(h.habitat_type || 'Unknown') +
               '</strong><span class="ai-confidence" style="--conf:' + conf + '">' + conf + '%</span></div>';
    html += '<table class="ai-habitat-table">';
    rows.forEach(function(r) {
      if (r[1]) html += '<tr><td>' + escapeHtml(r[0]) + '</td><td>' + escapeHtml(r[1]) + '</td></tr>';
    });
    html += '</table>';
    if (h.notes) html += '<p class="ai-notes">' + escapeHtml(h.notes) + '</p>';
    body.innerHTML = html;
    section.hidden = false;
  }

  analyzeBtn.addEventListener('click', function() {
    if (!selectedFile) { return; }
    if (analyzeBtn.disabled) { return; }
    openVerifyModal();
  });

  function openVerifyModal() {
    var overlay = document.getElementById('ai-modal-overlay');
    var check = document.getElementById('ai-verify-check');
    var errorEl = document.getElementById('ai-modal-error');
    check.checked = false;
    errorEl.hidden = true;
    overlay.hidden = false;
    check.focus();
  }

  function closeVerifyModal() {
    document.getElementById('ai-modal-overlay').hidden = true;
  }

  document.getElementById('ai-modal-cancel').addEventListener('click', closeVerifyModal);
  document.getElementById('ai-modal-overlay').addEventListener('click', function(e) {
    if (e.target === this) closeVerifyModal();
  });
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeVerifyModal();
  });

  document.getElementById('ai-modal-submit').addEventListener('click', function() {
    var check = document.getElementById('ai-verify-check');
    var errorEl = document.getElementById('ai-modal-error');
    if (!check.checked) {
      errorEl.hidden = false;
      return;
    }
    closeVerifyModal();
    runAnalysis();
  });

  function runAnalysis() {
    errorBox.hidden = true;
    errorBox.textContent = '';
    loading.hidden = false;
    results.hidden = false;
    clearResults();

    var formData = new FormData();
    formData.append('photo', selectedFile);
    formData.append('analysis_type', getAnalysisType());

    var controller = new AbortController();
    var timeoutId = setTimeout(function() { controller.abort(); }, 120000);

    fetch('/api/ai-identify', { method: 'POST', body: formData, signal: controller.signal })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
      .then(function(res) {
        clearTimeout(timeoutId);
        loading.hidden = true;
        if (!res.ok) {
          errorBox.textContent = res.data.error || 'Something went wrong. Please try again.';
          errorBox.hidden = false;
          return;
        }
        var d = res.data;
        if (d.species) renderSpecies(d.species, d.species_summary);
        if (d.habitat) renderHabitat(d.habitat);
      })
      .catch(function() {
        clearTimeout(timeoutId);
        loading.hidden = true;
        errorBox.textContent = 'Request timed out or failed. The AI service may be busy. Please try again.';
        errorBox.hidden = false;
      });
  }
})();
