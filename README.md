# 🚀 Founder Sourcing Pipeline

This is a repo to find startups funded by top venture capital firms.

### Step 1: Fork the Repository

Go to the GitHub repository and click the **Fork** button in the top right corner to create your own copy.

---

### Step 2: Enable GitHub Actions

> ⚠️ **Crucial:** By default, GitHub pauses automations on forked repositories.

1. Click the **Actions** tab at the top of your forked repository.
2. Click the green button that says **"I understand my workflows, go ahead and enable them."**

---

### Step 3: Add Your API Keys & Emails

1. Go to **Settings** > **Secrets and variables** > **Actions**.
2. Click **New repository secret** and add these 5 exact keys one by one:

* `GROQ_API_KEY` – Get this free from Groq's console.
* `TAVILY_API_KEY` – Get this free from Tavily's console.
* `SENDER_EMAIL` – The Gmail address sending the report.
* `SENDER_PASSWORD` – Your Google App Password (*not* your standard email password).
* `RECEIVER_EMAIL` – Where you want to receive the weekly report.

---

### Step 4: Jumpstart the Engine

1. Go back to the **Actions** tab.
2. Click **Weekly Founder Sourcing Pipeline** on the left menu.
3. Click the **Run workflow** dropdown on the right and hit the green button.

> 💡 **Note:** Once you run it manually this first time, the automation will trigger automatically every Monday at **10:00 AM IST**!