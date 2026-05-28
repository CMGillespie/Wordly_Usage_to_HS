# Wordly Usage to HubSpot Reconciler (v1.8)

This tool automates the extraction of account usage data from the Wordly Portal, calculates usage deltas (7-day and 30-day velocity), and enriches the data with HubSpot Contact and Company IDs for seamless CRM import.

## 📂 Project Structure
* `wordly_unified_master.py`: The main execution script.
* `/DATA`: **Crucial.** Stores historical CSVs. The script looks here to calculate usage deltas.
* `/UPLOAD`: Output folder. The final file for HubSpot import is generated here.
* `.gitignore`: Prevents sensitive credentials and raw data from being committed to Git.

## 🔐 Setup & Security
Before running, ensure the following two files exist in the root directory (they are ignored by Git):
1.  `wordly_creds.txt`: Contains `email,password` for the Wordly Portal.
2.  `HS_Service_key.txt`: Contains your HubSpot Private App Access Token.

## 🚀 How to Run
1.  Open Terminal and navigate to this folder.
2.  Execute the script:
    ```bash
    python3 wordly_unified_master.py
    ```
3.  **Phase 1 (Scraper):** A browser will open. It will automatically skip accounts with no data to save time.
4.  **Phase 2 (Enrichment):** The script will fetch ~170k contacts from HubSpot. Look for the "Heartbeat" counter in the terminal to track progress.

## 📥 Post-Run Workflow (The "Kirk" Steps)
Once the script says `✅ SUCCESS`:
1.  Go to the `/UPLOAD` folder.
2.  Take the newest `Wordly_Master_Import_YYYY-MM-DD.csv` and upload it to HubSpot.
3.  **Important:** After a successful upload, move that file from `/UPLOAD` to the `/DATA` folder. This ensures that tomorrow's run has a "yesterday" file to compare against for math.

## 🛠️ Troubleshooting
* **Timeout Errors:** If the portal is slow, the scraper might fail a specific account. It will log the failure and continue to the next one.
* **Missing Math:** If "Consumed Last 7 Days" is 0, ensure there is a CSV file at least 7 days old in the `/DATA` folder.