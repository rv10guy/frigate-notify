var event_id = document.querySelector('.container').getAttribute('data-event-id');

document.getElementById('event-image').src = '/api/events/' + event_id + '/snapshot.jpg';
document.getElementById('event-video').src = '/api/events/' + event_id + '/clip.mp4';

document.getElementById('delete-button').addEventListener('click', function() {
  fetch('http://192.168.142.4:5000/api/events/' + event_id, {
    method: 'DELETE'
  });
});

document.getElementById('send-button').addEventListener('click', function() {
  fetch('http://192.168.142.4:5000/api/events/' + event_id + '/plus', {
    method: 'POST'
  });
});

document.getElementById('save-button').addEventListener('click', function() {
  fetch('http://192.168.142.4:5000/api/events/' + event_id + '/retain', {
    method: 'POST'
  });
});

// More code will be added here for data retrieval and error handling