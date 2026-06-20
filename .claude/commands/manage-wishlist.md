Manage the shopping wishlist stored in Google Drive (file ID: `YOUR_WISHLIST_SHEET_ID`).

Parse $ARGUMENTS to determine the action:

## Actions

### `list`
Read the wishlist from Google Drive and display all items as a formatted table with columns:
Name | URL | Target Price | Notify Restock | Notify Price Drop | Last Price | In Stock | Last Checked

### `add "<name>" <url> <target_price> [notify_restock=true] [notify_price_drop=true]`
1. Read current wishlist CSV from Drive using `read_file_content`
2. Append a new row with the provided values (default notify flags to `true` if not specified, leave last_price/in_stock/last_checked empty)
3. Write the updated content back to Drive using `create_file` with the same file ID and title

### `remove "<name>"`
1. Read current wishlist CSV from Drive
2. Delete the row where `name` matches (case-insensitive)
3. Write back the updated content to Drive

### `update "<name>" price=<new_price>` or `update "<name>" notify_restock=<true/false>`
1. Read current wishlist CSV from Drive
2. Update the specified field for the matching row
3. Write back to Drive

## Important
- Always use file ID `YOUR_WISHLIST_SHEET_ID` for reading and writing
- When writing back, use `create_file` with `disableConversionToGoogleType: false` so it stays as a Google Sheet
- Confirm the action with a summary after completing
