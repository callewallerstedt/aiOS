(() => {
  const token = document.body.dataset.token;
  const cameraInput = document.querySelector('#camera-input');
  const libraryInput = document.querySelector('#library-input');
  const count = document.querySelector('#count');
  const status = document.querySelector('#status');
  const detail = document.querySelector('#detail');
  const queue = document.querySelector('#queue');
  let savedCount = Number(count.textContent) || 0;
  let pending = 0;

  function render() {
    count.textContent = String(savedCount);
    if (pending > 0) {
      status.textContent = `Sending ${pending} ${pending === 1 ? 'photo' : 'photos'}…`;
      detail.textContent = 'Keep this page open';
    } else {
      status.textContent = savedCount ? 'Ready for another photo' : 'Ready';
      detail.textContent = `${savedCount} ${savedCount === 1 ? 'photo' : 'photos'} sent`;
    }
  }

  async function upload(file) {
    if (!file) return;
    pending += 1;
    render();
    const row = document.createElement('div');
    row.className = 'queue-item';
    row.textContent = `Sending ${file.name || 'photo'}…`;
    queue.prepend(row);

    const form = new FormData();
    form.append('image', file, file.name || 'phone-photo.jpg');
    try {
      const response = await fetch(`/api/photo-drop/${encodeURIComponent(token)}/upload`, {
        method: 'POST',
        body: form,
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || 'Upload failed');
      savedCount = payload.count;
      row.textContent = `Sent ${payload.filename}`;
      window.setTimeout(() => row.remove(), 3500);
    } catch (error) {
      row.classList.add('error');
      row.textContent = `${file.name || 'Photo'}: ${error.message}. Tap Take photo to retry.`;
    } finally {
      pending -= 1;
      render();
    }
  }

  cameraInput.addEventListener('change', () => {
    const file = cameraInput.files && cameraInput.files[0];
    cameraInput.value = '';
    upload(file);
  });

  libraryInput.addEventListener('change', () => {
    const files = Array.from(libraryInput.files || []);
    libraryInput.value = '';
    files.forEach(upload);
  });

  render();
})();
