================================================================================
  Website Scraper - Wisnia Capital
================================================================================

WHAT DOES THIS DO?
------------------
This program visits a list of renewable energy company websites, reads all
the text on those websites, and saves that text into simple text files on
your computer.

Think of it like copying and pasting every page of a website into a Word
document -- but it does it automatically for hundreds of websites at once.

After the text is saved, a separate AI program reads those text files and
figures out which companies might be a good match for solar energy deals.


WHAT DO YOU NEED BEFORE YOU START?
----------------------------------
1. A computer with Python installed (python.org)
2. Internet connection
3. A list of websites you want to scrape (you can just type them in)

To install the required add-ons, open a terminal (Command Prompt) and type:

   pip install flask requests beautifulsoup4 lxml tldextract chardet tenacity pypdf pandas openpyxl trafilatura


HOW TO USE IT (Step by Step)
----------------------------

  EASIEST WAY: The Web App
  -------------------------
  1. Open a terminal (Command Prompt)

  2. Navigate to this folder by typing:
       cd C:\Users\Temp\Documents\ChatGPTSc

  3. Type this and press Enter:
       python app.py

  4. Your web browser should open automatically.
     If it doesn't, open your browser and go to:  http://localhost:5000

  5. You'll see a text box. Paste your list of websites in there.
     Example:
       solarcompany.com
       greenenergy.org
       sunpowergroup.com

  6. Click the "Scrape & Save" button.

  7. Wait. You'll see a progress bar and live updates showing which
     websites are being scraped. This can take a few minutes depending
     on how many websites you entered.

  8. When it's done, your text files will be in a folder called:
       ALL_TEXT_FILES

     Inside that folder there will be one text file per website.
     For example:
       ALL_TEXT_FILES/solarcompany.com.txt
       ALL_TEXT_FILES/greenenergy.org.txt


  ALTERNATIVE: Command Line (for advanced users)
  ------------------------------------------------
  If you have a spreadsheet (.xlsx or .csv) with a list of websites:

    python scrape_sites_to_text.py --input yourfile.xlsx

  The scraped text will appear in a folder called "scraped_text".


WHAT ARE ALL THESE FILES?
-------------------------
  app.py
    The main program. This is what you run to start the web app.

  scraper_core.py
    A separate scraping engine used by scraper_server.py (the older
    web app). If you're using app.py, this file isn't used at all.
    If you're using scraper_server.py, this is the engine behind it.

  scraper_server.py
    An older version of the web app. You can ignore this.

  scraper_ui.html
    The webpage design for the older version. You can ignore this too.

  scrape_sites_to_text.py
    A version you can run from the command line if you prefer typing
    commands instead of using the web app.

  text_files_to one folder.py
    A helper tool that takes scraped files from different folders and
    puts them all into one folder. You only need this if your files
    ended up in a messy folder structure.

  ALL_TEXT_FILES/
    This is where the finished text files go. One file per website.

  scraped_text/
    Another output folder used by the command-line version.

  ReadMe.txt
    This file. You're reading it right now!


HOW DOES THE SCRAPER WORK? (Simple Explanation)
------------------------------------------------
  1. You give it a list of website addresses.

  2. For each website, it visits the homepage.

  3. It finds all the links on that page and follows them
     (like clicking through a website), up to 25 pages deep.

  4. On each page, it pulls out just the readable text
     (ignoring ads, menus, code, etc.).

  5. It combines all the text from that website into one file.

  6. It saves that file with the website's name
     (e.g., "solarcompany.com.txt").

  7. It does all of this for every website in your list,
     working on several websites at the same time to go faster.

  8. It's polite -- it waits a little between each page visit
     so it doesn't overwhelm the websites.


THE BIG PICTURE
---------------
  Step 1: Run this scraper  -->  Get text files of company websites
  Step 2: Feed those text files to an AI program (separate repo)
  Step 3: The AI scores each company for solar deal fit
  Step 4: Results saved to a spreadsheet for the team to review


SETTINGS YOU CAN CHANGE (in the web app)
-----------------------------------------
  Max Pages        How many pages to visit per website (default: 25)
  Max Depth        How many links deep to follow (default: 2)
  Timeout          Max seconds to spend on one website (default: 90)
  Workers          How many websites to scrape at the same time (default: 6)


TROUBLESHOOTING
---------------
  "python is not recognized"
    Python isn't installed. Download it from python.org and install it.
    Make sure to check "Add Python to PATH" during installation.

  "ModuleNotFoundError: No module named 'flask'"
    You need to install the add-ons. Run the pip install command
    listed in the "What Do You Need" section above.

  The browser doesn't open
    Manually open your browser and go to http://localhost:5000

  A website shows "failed" or "0 pages"
    Some websites block scrapers or are offline. That's normal.
    The scraper will skip those and move on to the next one.

  It's running very slowly
    Scraping is slow by design -- it waits between page visits to
    be respectful to the websites. For 100+ sites, expect it to
    take 30-60 minutes.


ORIGINAL SOURCE DATA
--------------------
  The list of companies comes from:
    C:\Users\Temp\Wisnia Capital\Wisnia Capital - MASTER - Documents\
    Lists\Solar Investors\USearch - Complete List of Renewable Energy
    Companies.xlsx (Sheet 3)

  The final deal fit results go to:
    C:\Users\Temp\Downloads\solon deal fit_gptscraper.csv



    ================================================================================
  Wisnia Capital - Renewable Energy Website Scraper
================================================================================

PURPOSE
-------
Scrapes websites from a list of renewable energy companies and consolidates
the text content into organized output files. The scraped text is then used
with AI (Claude/Cursor) in a separate repo to match solar deal fit.

Source data:
  C:\Users\Temp\Wisnia Capital\Wisnia Capital - MASTER - Documents\Lists\
  Solar Investors\USearch - Complete List of Renewable Energy Companies.xlsx
  (Sheet 3)

Output results file:
  C:\Users\Temp\Downloads\solon deal fit_gptscraper.csv


FILES IN THIS REPO
------------------
  app.py                    - Flask web server with integrated scraper + SSE
                              streaming (primary web interface)
  scraper_core.py           - Core scraping engine (reusable module)
  scraper_server.py         - Alternative Flask server wrapping scraper_core.py
  scraper_ui.html           - Web UI for scraper_server.py
  scrape_sites_to_text.py   - Command-line scraper (reads CSV/Excel files)
  text_files_to one folder.py - Utility to flatten nested output into one folder


QUICK START
-----------

  Option 1: Web UI via app.py (Recommended)
  ------------------------------------------
  1. Install dependencies:
       pip install flask requests beautifulsoup4 lxml tldextract chardet
       pip install tenacity pypdf pandas openpyxl trafilatura

  2. Run the server:
       python app.py

  3. Open http://localhost:5000 in your browser

  4. Paste domain names into the text area, adjust settings, click "Scrape & Save"

  5. Watch real-time progress via the activity log and stats dashboard

  6. Output files appear in ALL_TEXT_FILES/ (one .txt per domain)


  Option 2: Web UI via scraper_server.py
  ---------------------------------------
  1. Install same dependencies as above

  2. Run:
       python scraper_server.py

  3. Open http://localhost:5000 - uses scraper_ui.html as the frontend

  4. Same workflow: paste domains, click start, monitor progress


  Option 3: Command-Line via scrape_sites_to_text.py
  ---------------------------------------------------
  Run:
    python scrape_sites_to_text.py --input companies.xlsx

  Full options:
    python scrape_sites_to_text.py ^
      --input companies.xlsx         (required: path to .csv or .xlsx)
      --website_column "Website"     (auto-detected if omitted)
      --output_dir scraped_text      (default: scraped_text)
      --max_pages 25                 (pages per site, default: 25)
      --max_depth 2                  (crawl depth, default: 2)
      --max_seconds_per_site 90      (timeout per site, default: 90)
      --max_workers 6                (parallel threads, default: 6)
      --save_per_page                (save individual page files)
      --no_resume                    (re-scrape already done sites)
      --no_subdomains                (stay on exact domain only)

  Output structure:
    scraped_text/
      <domain>/merged/<domain>.txt   - Consolidated text per site
      <domain>/pages/                - Individual page files (if --save_per_page)
    logs/
      scrape_run_<timestamp>.log     - Run log
      scrape_run_<timestamp>.jsonl   - Detailed JSON records
      summary_<timestamp>.csv        - Summary statistics


  Option 4: Use scraper_core.py Programmatically
  ------------------------------------------------
    from scraper_core import ScrapeConfig, SessionFactory, crawl_site

    cfg = ScrapeConfig(
        input_path="companies.xlsx",
        output_dir="output",
        max_pages_per_site=25,
        max_depth=2
    )
    session_factory = SessionFactory(cfg)
    site_result, page_results = crawl_site("example.com", cfg, session_factory)


DEPENDENCIES
------------
  flask              - Web framework
  requests           - HTTP client
  beautifulsoup4     - HTML parsing (fallback extractor)
  lxml               - HTML parser backend
  tldextract         - Domain/TLD extraction
  chardet            - Character encoding detection
  tenacity           - Retry logic with exponential backoff
  pypdf              - PDF text extraction
  pandas             - CSV/Excel reading
  openpyxl           - Excel file support
  trafilatura        - Advanced web text extraction (primary extractor)


CONFIGURATION DEFAULTS
----------------------
  Max pages per site:       25
  Max crawl depth:          2
  Timeout per site:         90 seconds
  Max response size:        7 MB
  Parallel workers:         6
  Polite delay per request: 0.4 - 1.2 seconds (random)
  Connect timeout:          10 seconds
  Read timeout:             20 seconds
  TLS verification:         Enabled


OUTPUT DIRECTORIES
------------------
  ALL_TEXT_FILES/      - Flat directory with one .txt file per domain (final output)
  scraped_text/        - Nested structure with per-site folders
  scraped_text_work/   - Working directory used by scraper_server.py
  logs/                - Run logs, JSONL records, and summary CSVs


HOW THE SCRAPER WORKS
---------------------
  1. Takes a list of domains (from UI, CLI, or Excel file)
  2. For each domain, performs BFS (breadth-first) crawling:
     - Starts at the homepage
     - Discovers and follows internal links up to max_depth
     - Prioritizes important pages (about, portfolio, projects, team, etc.)
     - Skips login, cart, privacy, and other non-content pages
     - Respects per-site time limits
  3. Extracts clean text from each page:
     - Primary: Trafilatura (better at extracting article content)
     - Fallback: BeautifulSoup (strips tags, normalizes whitespace)
     - Also handles PDF files via PyPDF
  4. Merges all page text into a single file per domain
  5. Saves output to ALL_TEXT_FILES/ directory

  The scraper is polite: random delays between requests, respects timeouts,
  and identifies itself via a custom User-Agent string.


WORKFLOW
--------
  1. Scrape websites using this repo  -->  ALL_TEXT_FILES/*.txt
  2. Feed scraped text to AI deal matching repo (Claude/Cursor)
  3. AI produces deal fit scores  -->  solon deal fit_gptscraper.csv