-- open_boxes never exceeds boxes, and taken is exactly the difference.
select state_id, boxes, open_boxes, taken
from {{ ref('fct_night_boxes') }}
where open_boxes > boxes or taken <> boxes - open_boxes or boxes < 0
