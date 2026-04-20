javascript:(function(){
  const apiURL = 'apiUrl'; 
  const apiKey = (window.techwatch_config && window.techwatch_config.apiKey) || '';
  const data = { url: window.location.href };

  fetch(apiURL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-API-Key': apiKey
    },
    body: JSON.stringify(data)
  })
  .then(response => {
    if (response.ok) { alert('URL sent successfully!'); }
    else { alert('Error sending URL.'); }
  })
  .catch(error => alert('Network error: ' + error));
})();
