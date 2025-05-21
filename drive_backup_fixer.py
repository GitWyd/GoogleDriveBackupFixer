import os
import io
import json
import datetime # For logging timestamp
import time     # For temporary filename uniqueness
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

# --- Configuration ---
LOCAL_BACKUP_PATH = "/path/to/your/local/google-drive-backup"  # !!! USER HAS SET THIS !!!
DEMO_MODE = True  # SET TO False TO PERFORM ACTUAL FILE OPERATIONS AND DOWNLOADS

SIZE_THRESHOLD_BYTES = 256
EXCLUDED_EXTENSIONS = [".ini"] # e.g. [".ini", ".DS_Store"]
LOST_FILES_LOG = "lost_or_failed_files.txt"
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.json'
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

EXPORT_MIMETYPES = {
    'application/vnd.google-apps.document': {
        'mimeType': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'extension': '.docx'
    },
    'application/vnd.google-apps.spreadsheet': {
        'mimeType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'extension': '.xlsx'
    },
    'application/vnd.google-apps.presentation': {
        'mimeType': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'extension': '.pptx'
    },
    'application/vnd.google-apps.drawing': {
        'mimeType': 'image/png',
        'extension': '.png'
    },
}

# --- Permission Check & Helper Functions ---
def check_write_permissions(directory_path):
    """Checks if the script can write and delete a file in the given directory."""
    if not os.path.isdir(directory_path):
        print(f"Error: Write check failed. Path '{directory_path}' is not a directory.")
        return False
    
    # More unique temp filename
    temp_filename = os.path.join(directory_path, f".script_write_test_{int(time.time())}.tmp")
    try:
        with open(temp_filename, 'w') as f:
            f.write("test_write_permissions")
        os.remove(temp_filename)
        print(f"Successfully verified write/delete permissions in '{directory_path}'.")
        return True
    except Exception as e:
        print(f"ERROR: Could not verify write/delete permissions in '{directory_path}'.")
        print(f"       Attempted to create/delete '{temp_filename}'.")
        print(f"       Encountered error: {e}")
        if isinstance(e, PermissionError) or (hasattr(e, 'errno') and e.errno == 1): # EPERM
             print("       This is likely a file system permission issue (e.g., directory not writable by user,")
             print("       or on macOS, the directory or its parents might be 'locked' - check Finder's Get Info).")
             print("       If locked on macOS, you may need to use 'chflags -R nouchg \"your_backup_path\"' in Terminal.")
        return False

# (get_drive_service, find_small_files, get_id_from_google_shortcut_file, search_drive_file remain largely the same)
# --- Google Drive Authentication ---
def get_drive_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"Error refreshing token: {e}. Please re-authenticate by deleting '{TOKEN_FILE}'.")
                creds = None
        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
    try:
        service = build('drive', 'v3', credentials=creds)
        print("Successfully connected to Google Drive API.")
        return service
    except HttpError as error:
        print(f"An error occurred building the Drive service: {error}")
    except Exception as e:
        print(f"An unexpected error occurred during Drive service build: {e}")
    return None

# --- Local File Operations ---
def find_small_files(backup_path, threshold, excluded_exts):
    small_files = []
    for root, _, files in os.walk(backup_path):
        for filename in files:
            filepath = os.path.join(root, filename)
            try:
                if os.path.isfile(filepath) and not os.path.islink(filepath):
                    filesize = os.path.getsize(filepath)
                    _, ext = os.path.splitext(filename)
                    # Add .DS_Store to common exclusions for macOS if not already there
                    common_excluded = excluded_exts + [".ds_store"]
                    if filesize < threshold and ext.lower() not in common_excluded:
                        small_files.append(filepath)
            except OSError as e:
                print(f"Warning: Could not access or get size of {filepath}: {e}")
    return small_files

def get_id_from_google_shortcut_file(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
            possible_keys = ['doc_id', 'file_id', 'id', 'resource_id']
            for key in possible_keys:
                if key in data and isinstance(data[key], str):
                    return data[key]
            print(f"  Could not find a known ID key in JSON of {filepath}. Data: {data}")
    except json.JSONDecodeError:
        print(f"  {filepath} is not a valid JSON file (or not a Google shortcut). Cannot extract ID.")
    except IOError as e:
        print(f"  Could not read {filepath} to extract ID: {e}")
    except Exception as e:
        print(f"  Unexpected error parsing shortcut {filepath} for ID: {e}")
    return None

# --- Google Drive Operations ---
def search_drive_file(service, file_id=None, filename=None):
    try:
        if file_id:
            print(f"  Searching Drive for file ID: {file_id}")
            file_metadata = service.files().get(
                fileId=file_id,
                fields="id, name, mimeType, capabilities(canDownload), shared, owners, parents,webViewLink"
            ).execute()
            return file_metadata
        elif filename:
            print(f"  Searching Drive for filename: '{filename}' (this can be ambiguous)")
            query_filename = filename.replace("'", "\\'")
            query = f"name = '{query_filename}' and trashed = false"
            results = service.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name, mimeType, capabilities(canDownload), shared, owners, parents,webViewLink)',
                pageSize=10 # Be careful with pageSize if ambiguity is high
            ).execute()
            items = results.get('files', [])
            if not items:
                print(f"  No file found with name '{filename}'.")
                return None
            if len(items) > 1:
                print(f"  Warning: Found {len(items)} files named '{filename}'. Using the first one found: {items[0].get('name')} (ID: {items[0].get('id')})")
            return items[0]
    except HttpError as error:
        # Specifically check for 404 when searching by ID
        if file_id and error.resp.status == 404:
            print(f"  File with ID '{file_id}' not found on Google Drive (404 Error).")
        else:
            print(f"  API Error searching for file ({file_id or filename}): {error}")
    except Exception as e:
        print(f"  Unexpected error searching for file ({file_id or filename}): {e}")
    return None


def download_drive_file(service, drive_file_metadata, local_placeholder_path):
    file_id = drive_file_metadata['id']
    original_drive_name = drive_file_metadata['name']
    drive_mime_type = drive_file_metadata['mimeType']
    local_dir = os.path.dirname(local_placeholder_path)
    
    can_download_directly = drive_file_metadata.get('capabilities', {}).get('canDownload', False)
    request = None
    new_filename_on_disk = original_drive_name
    is_export = False

    # Specific handling for Google Forms
    if drive_mime_type == 'application/vnd.google-apps.form':
        message = (f"  File '{original_drive_name}' (ID: {file_id}) is a Google Form. "
                   f"Google Forms cannot be directly exported to a simple file format by this script. "
                   f"Consider linking responses to a Google Sheet for backup, or use Google Takeout.")
        print(message)
        return None, "Google Form (unsupported for useful export by this script)"

    if drive_mime_type in EXPORT_MIMETYPES:
        export_details = EXPORT_MIMETYPES[drive_mime_type]
        export_mime_type = export_details['mimeType']
        new_extension = export_details['extension']
        base_name, _ = os.path.splitext(original_drive_name)
        new_filename_on_disk = base_name + new_extension
        print(f"  File is a Google Workspace type. Preparing to export '{original_drive_name}' as {new_filename_on_disk} ({export_mime_type}).")
        try:
            request = service.files().export_media(fileId=file_id, mimeType=export_mime_type)
            is_export = True
        except HttpError as e:
            if e.resp.status == 403 and "exportSizeLimitExceeded" in str(e.content):
                 print(f"  API Error: File '{original_drive_name}' (ID: {file_id}) is too large to be exported by the API.")
                 return None, "File too large for API export"
            print(f"  API error preparing export for {file_id} ({original_drive_name}): {e}")
            return None, f"API error during export prep ({e.resp.status})"

    elif 'application/vnd.google.colaboratory' == drive_mime_type and can_download_directly:
        print(f"  File '{original_drive_name}' is a Google Colaboratory file. Preparing to download directly (as .ipynb).")
        new_filename_on_disk = original_drive_name if original_drive_name.lower().endswith('.ipynb') else original_drive_name + '.ipynb'
        try:
            request = service.files().get_media(fileId=file_id)
        except HttpError as e:
            print(f"  API error preparing Colab download for {file_id} ({original_drive_name}): {e}")
            return None, f"API error during Colab download prep ({e.resp.status})"

    elif can_download_directly:
        print(f"  File '{original_drive_name}' (Type: {drive_mime_type}) is a standard file. Preparing to download directly.")
        try:
            request = service.files().get_media(fileId=file_id)
        except HttpError as e:
            print(f"  API error preparing direct download for {file_id} ({original_drive_name}): {e}")
            return None, f"API error during direct download prep ({e.resp.status})"
    else:
        message = (f"  File '{original_drive_name}' (ID: {file_id}, Type: {drive_mime_type}) "
                   f"cannot be exported with current rules and direct download is not permitted/possible.")
        print(message)
        return None, "Cannot export with defined rules and not directly downloadable"

    if request:
        new_filepath = os.path.join(local_dir, new_filename_on_disk)
        if os.path.exists(new_filepath) and new_filepath.lower() != local_placeholder_path.lower():
            if DEMO_MODE:
                 print(f"  [DEMO MODE] A file named '{new_filename_on_disk}' already exists in '{local_dir}'. Would append '_downloaded'.")
            else:
                print(f"  Warning: A file named '{new_filename_on_disk}' already exists in '{local_dir}'. Appending '_downloaded'.")
                base, ext = os.path.splitext(new_filename_on_disk)
                new_filename_on_disk = f"{base}_downloaded_{int(time.time())}{ext}" # Ensure uniqueness
                new_filepath = os.path.join(local_dir, new_filename_on_disk)

        print(f"  Target local path: {new_filepath}")
        
        if DEMO_MODE:
            print(f"  [DEMO MODE] Would attempt to {'export' if is_export else 'download'} file ID {file_id} ('{original_drive_name}') to '{new_filepath}'.")
            print(f"  [DEMO MODE] Would rename placeholder '{local_placeholder_path}' to '{local_placeholder_path}.placeholder_original'.")
            return new_filepath, None

        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        placeholder_backup_name = local_placeholder_path + ".placeholder_original"
        renamed_placeholder = False

        try:
            try:
                if os.path.exists(placeholder_backup_name): os.remove(placeholder_backup_name) # remove old backup
                os.rename(local_placeholder_path, placeholder_backup_name)
                renamed_placeholder = True
                print(f"  Renamed placeholder '{local_placeholder_path}' to '{placeholder_backup_name}'.")
            except OSError as e_rename:
                err_msg_rename = f"Warning: Could not rename placeholder '{local_placeholder_path}': {e_rename}."
                if hasattr(e_rename, 'errno') and e_rename.errno == 1: # EPERM
                    err_msg_rename += " This often means the file is locked or permissions are insufficient on the source file. (macOS: check 'uchg' flag)."
                print(f"  {err_msg_rename} Download will proceed, but original placeholder remains.")
                # Do not return here, proceed to download, but the old file won't be "archived"

            while not done:
                status, done = downloader.next_chunk()
                if status: print(f"    Download {int(status.progress() * 100)}%.")
            
            with open(new_filepath, 'wb') as f:
                f.write(fh.getvalue())
            print(f"  Successfully saved: {new_filepath}")
            return new_filepath, None
        except OSError as e_os_write: # Specifically for write error
            err_msg = f"OS error writing file '{new_filepath}': {e_os_write}."
            if hasattr(e_os_write, 'errno') and e_os_write.errno == 1: # EPERM
                 err_msg += " This could be due to a file lock (e.g., 'uchg' flag on macOS for the target path/name) or insufficient permissions in the target directory."
            print(f"    {err_msg}")
            if renamed_placeholder:
                 try: os.rename(placeholder_backup_name, local_placeholder_path); print(f"  Restored placeholder.")
                 except Exception as e_restore: print(f"  Could not restore placeholder: {e_restore}")
            return None, err_msg
        except HttpError as error: # For GDrive API errors during download
            print(f"    An API error occurred during download/export for {file_id}: {error}")
            if renamed_placeholder:
                 try: os.rename(placeholder_backup_name, local_placeholder_path); print(f"  Restored placeholder.")
                 except Exception as e_restore: print(f"  Could not restore placeholder: {e_restore}")
            return None, f"Download/Export API Error ({error.resp.status if hasattr(error,'resp') else 'Unknown'})"
        except Exception as e: # Other errors
            print(f"    An unexpected error during download/write operation: {e}")
            if renamed_placeholder:
                 try: os.rename(placeholder_backup_name, local_placeholder_path); print(f"  Restored placeholder.")
                 except Exception as e_restore: print(f"  Could not restore placeholder: {e_restore}")
            return None, f"Unexpected download/write error: {str(e)}"
    return None, "Request object was not created (no download/export path)"

# --- Main Logic ---
def main():
    print(f"Script starting at: {datetime.datetime.now().isoformat()}")
    if DEMO_MODE:
        print("\n**************************************************")
        print("*** SCRIPT IS RUNNING IN DEMO MODE.            ***")
        print("*** NO LOCAL FILES WILL BE MODIFIED OR CREATED. ***")
        print("**************************************************\n")

    print(f"Local backup path: {LOCAL_BACKUP_PATH}")
    print(f"File size threshold: < {SIZE_THRESHOLD_BYTES} bytes")
    print(f"Excluded extensions: {EXCLUDED_EXTENSIONS}")

    if LOCAL_BACKUP_PATH == "/path/to/your/local/google-drive-backup" and not DEMO_MODE: # Added not DEMO_MODE
        print("\nCRITICAL ERROR: 'LOCAL_BACKUP_PATH' is set to the default placeholder.")
        print("Please update this variable in the script to your actual backup directory before running in non-DEMO mode.")
        return
    if not os.path.isdir(LOCAL_BACKUP_PATH):
        print(f"Error: Local backup path '{LOCAL_BACKUP_PATH}' does not exist or is not a directory.")
        return

    if not DEMO_MODE:
        print("\nPerforming pre-run write permission check...")
        if not check_write_permissions(LOCAL_BACKUP_PATH):
            print(f"\nCritical: Write permission check failed for the root backup directory: '{LOCAL_BACKUP_PATH}'.")
            print("The script needs to be able to create and delete files in this directory.")
            print("Please check directory permissions, or if on macOS, check for 'locked' folders (use 'chflags -R nouchg \"path\"').")
            print("Aborting script as file operations would likely fail.")
            return
        else:
            print("Pre-run write permission check passed for the root backup directory.")


    drive_service = get_drive_service()
    if not drive_service:
        print("Could not connect to Google Drive. Exiting.")
        return

    print("\nScanning for small files (potential placeholders)...")
    candidate_files = find_small_files(LOCAL_BACKUP_PATH, SIZE_THRESHOLD_BYTES, EXCLUDED_EXTENSIONS)

    if not candidate_files:
        print("No files found matching the criteria. Your backup might be complete or criteria are too restrictive.")
        return

    print(f"Found {len(candidate_files)} candidate files to check against Google Drive.")
    lost_and_failed_files = []
    successfully_processed_count = 0
    simulated_download_count = 0

    for i, local_path in enumerate(candidate_files):
        print(f"\n--- Processing file {i+1}/{len(candidate_files)}: {local_path} ---")
        file_id = None
        filename_to_search = os.path.basename(local_path)
        base_local_name, local_ext = os.path.splitext(filename_to_search)

        # Prioritize getting ID from Google shortcut files
        if local_ext.lower() in ['.gdoc', '.gsheet', '.gslides', '.gform', '.gdraw', '.gtable', '.gjam']:
            print(f"  Detected Google shortcut type extension: {local_ext}")
            file_id = get_id_from_google_shortcut_file(local_path)
            if file_id:
                print(f"  Extracted Google Drive File ID: {file_id}")
            else:
                # If ID extraction from shortcut fails, use the base name of the shortcut for searching.
                # e.g., if "MyDoc.gdoc" (as json) is malformed, search for "MyDoc"
                filename_to_search = base_local_name 
                print(f"  Could not get ID from shortcut file. Will search by inferred name: '{filename_to_search}'")
        
        drive_file_info = None
        if file_id:
            drive_file_info = search_drive_file(drive_service, file_id=file_id)
        
        if not drive_file_info: # Fallback to name search if ID search failed or no ID was available
            # If it wasn't a Google shortcut type, filename_to_search is already os.basename(local_path)
            # If it was a shortcut but ID extraction failed, filename_to_search is base_local_name
            print(f"  No file found by ID (or no ID extracted/available). Trying search by name: '{filename_to_search}'")
            drive_file_info = search_drive_file(drive_service, filename=filename_to_search)

        if drive_file_info:
            drive_link = drive_file_info.get('webViewLink', 'N/A')
            print(f"  Found on Drive: '{drive_file_info['name']}' (ID: {drive_file_info['id']}, Type: {drive_file_info['mimeType']}, Link: {drive_link})")
            
            if 'application/vnd.google-apps.folder' in drive_file_info['mimeType']:
                print(f"  The item found on Drive is a FOLDER. Skipping for placeholder '{local_path}'.")
                lost_and_failed_files.append(f"{local_path} (Reason: Placeholder linked to a FOLDER on Drive: '{drive_file_info['name']}' ID: {drive_file_info['id']})")
                continue

            downloaded_path_or_simulated, error_msg = download_drive_file(drive_service, drive_file_info, local_path)
            
            if downloaded_path_or_simulated and not error_msg:
                if DEMO_MODE: simulated_download_count +=1
                else: successfully_processed_count += 1
            else:
                lost_and_failed_files.append(f"{local_path} (Drive ID: {drive_file_info.get('id','N/A')}, Name: {drive_file_info.get('name','N/A')}, Reason: {error_msg})")
        else:
            # This 'else' means neither ID search (if applicable) nor name search found the file.
            search_term_logged = file_id if file_id else filename_to_search
            print(f"  File corresponding to '{local_path}' (searched as '{search_term_logged}') not found on Google Drive.")
            lost_and_failed_files.append(f"{local_path} (Reason: Not found on Google Drive using ID '{file_id if file_id else 'N/A'}' or name '{filename_to_search}')")

    print("\n--- Script Finished ---")
    if DEMO_MODE:
        print(f"Total candidate files checked: {len(candidate_files)}")
        print(f"Simulated downloads/exports: {simulated_download_count}")
        print(f"Files that would be logged as lost, failed, or needing manual review: {len(lost_and_failed_files)}")
    else:
        print(f"Total candidate files checked: {len(candidate_files)}")
        print(f"Successfully downloaded/exported: {successfully_processed_count}")
        print(f"Files lost, failed, or needing manual review: {len(lost_and_failed_files)}")

    if lost_and_failed_files:
        print(f"\nWriting details of {len(lost_and_failed_files)} problematic files to '{LOST_FILES_LOG}'...")
        try:
            with open(LOST_FILES_LOG, 'w', encoding='utf-8') as f:
                f.write(f"Report generated on: {datetime.datetime.now().isoformat()}\n")
                f.write(f"DEMO MODE ACTIVE: {DEMO_MODE}\n")
                f.write(f"Local Backup Path Scanned: {os.path.abspath(LOCAL_BACKUP_PATH)}\n")
                f.write(f"Size Threshold: < {SIZE_THRESHOLD_BYTES} bytes\n")
                f.write(f"Excluded Extensions: {', '.join(EXCLUDED_EXTENSIONS)}\n\n")
                f.write("--- Files Not Found, Failed to Download/Export, or Requiring Manual Attention ---\n")
                for item in lost_and_failed_files:
                    f.write(f"{item}\n")
            print(f"Log file '{LOST_FILES_LOG}' created successfully.")
            print("Please review this log. For local permission errors, you may need to adjust folder/file permissions or unlock them.")
            print("For 'File too large' errors, manual download from Google Drive website is needed for those specific files.")
            print("For 'File not found' errors, the file may have been deleted or moved on Google Drive.")
        except IOError as e:
            print(f"Error: Could not write to log file '{LOST_FILES_LOG}': {e}")
    elif len(candidate_files) > 0 :
         print("\nAll candidate files were successfully processed or accounted for (no items in lost/failed list).")
    else:
        print("\nNo candidate files to process.")

if __name__ == '__main__':
    main()