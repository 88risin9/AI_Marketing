'use strict';
(() => {
  const token = location.pathname.split('/').filter(Boolean).at(-1);
  const endpoint = `/api/public/pages/${encodeURIComponent(token)}`;
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let products = [];
  let submissionId = Array.from(crypto.getRandomValues(new Uint8Array(20)), x => x.toString(16).padStart(2, '0')).join('');
  const labels = {poles: 'Poles', current_a: 'Rated current (A)', voltage_v: 'Rated voltage (V)', curve: 'Trip curve', breaking_ka: 'Breaking capacity (kA)'};

  async function api(url, options = {}) {
    const response = await fetch(url, {credentials: 'omit', ...options});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Request could not be completed. Please try again.');
    return result;
  }
  function error(target, message) {
    $(target).textContent = message;
    $(target).hidden = false;
  }
  async function load() {
    try {
      const result = await api(endpoint);
      const page = result.page;
      products = page.products;
      $('page-title').textContent = page.title;
      $('page-intro').textContent = page.intro;
      $('demo-notice').hidden = !page.demo;
      $('products').innerHTML = products.map((product, index) => `<article class="product-card">
        <div class="product-top"><span class="product-number">${String(index + 1).padStart(2, '0')}</span><span class="category">${escape(product.category || 'Product')}</span></div>
        <h3>${escape(product.name_en || product.model || 'Product details')}</h3>
        <p class="model">${escape(product.model || 'Model to be confirmed')}</p>
        <dl>${Object.entries(labels).map(([key, label]) => `<div><dt>${label}</dt><dd class="${product.specs[key] ? '' : 'unknown'}">${escape(product.specs[key] || 'To be confirmed')}</dd></div>`).join('')}
        <div><dt>Unit</dt><dd>${escape(product.unit || 'To be confirmed')}</dd></div></dl>
        ${product.source_updated_at ? `<p class="source-date">Information date: ${escape(product.source_updated_at)}</p>` : '<p class="source-date">Information date: to be confirmed</p>'}
        <button type="button" class="choose-product" data-product="${index}">Enquire about this model <span aria-hidden="true">↗</span></button>
        </article>`).join('');
      $('model-options').innerHTML = products.filter(x => x.model).map(x => `<option value="${escape(x.model)}">${escape(x.name_en)}</option>`).join('');
      $('catalog').hidden = false;
      $('request-section').hidden = false;
    } catch (exc) {
      $('page-intro').textContent = 'The page may be awaiting review or no longer available.';
      error('load-error', exc.message || 'This procurement page could not be loaded.');
    }
  }
  $('products').addEventListener('click', event => {
    const button = event.target.closest('[data-product]');
    if (!button) return;
    const product = products[Number(button.dataset.product)];
    const form = $('inquiry-form');
    form.elements.model.value = product.model || product.name_en;
    if (!form.elements.unit.value) form.elements.unit.value = product.unit;
    $('request-section').scrollIntoView({behavior: 'smooth', block: 'start'});
    form.elements.requirements.focus({preventScroll: true});
  });
  $('inquiry-form').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    $('form-error').hidden = true;
    if (!form.reportValidity()) return;
    const fields = new FormData(form);
    if (!String(fields.get('name') || '').trim() && !String(fields.get('company') || '').trim()) {
      error('form-error', 'Please enter your contact name or company.'); return;
    }
    const file = form.elements.file.files[0];
    if (!String(fields.get('model') || '').trim() && !String(fields.get('requirements') || '').trim() && !file) {
      error('form-error', 'Enter a model, describe your requirements, or upload a purchase list.'); return;
    }
    if (file && file.size > 1024 * 1024) {
      error('form-error', 'Purchase list must be at most 1 MB.'); return;
    }
    fields.set('submission_id', submissionId);
    $('submit-button').disabled = true;
    $('submit-button').textContent = 'Submitting…';
    try {
      // Obtain a fresh token without clearing the buyer’s text or upload.
      const current = await api(endpoint);
      const result = await api(`${endpoint}/inquiries`, {method: 'POST', body: fields, headers: {'X-Buyer-CSRF': current.csrf_token}});
      form.hidden = true;
      $('receipt-message').textContent = result.message;
      $('receipt').hidden = false;
      $('receipt').scrollIntoView({behavior: 'smooth', block: 'center'});
    } catch (exc) {
      error('form-error', exc.message || 'The request could not be saved. Your form is unchanged; please try again.');
    } finally {
      $('submit-button').disabled = false;
      $('submit-button').textContent = 'Submit enquiry ↗';
    }
  });
  load();
})();
