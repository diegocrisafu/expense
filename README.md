# Expense Tracker App

This project is a lightweight **React** application for tracking personal expenses in the browser.  It allows you to add expenses with a date, description, category and amount, filter them by category and calculate the total.  Data is stored locally using the Web Storage API (`localStorage`), so your entries persist across page reloads.

## Features

* **Add expenses** – Use the form at the top of the page to record a new expense with a date, description, category and amount.
* **Delete expenses** – Remove an entry from the list using the trash icon in the actions column.
* **Define categories** – The default categories are **General**, **Food**, **Transport**, **Entertainment**, **Utilities** and **Health**, but you can easily add your own by editing the `<option>` elements in the form.
* **Filter by category** – Select a category from the drop‑down filter to view only matching expenses.
* **Real‑time totals** – The total of the currently displayed expenses updates automatically as you add, delete or filter entries.
* **Pie chart visualisation** – A Chart.js pie chart summarises your spending by category to give a quick overview of where your money goes.
* **Persistent storage** – Data persists across page reloads using your browser’s `localStorage` API.

## Getting Started

Open `index.html` in any modern browser to start using the app.  There is no build process since the page loads React and Babel from CDNs and compiles the JSX in the browser.

Alternatively, once GitHub Pages is enabled for this repository you will be able to access the live app at:

```
https://diegocrisafu.github.io/expense/
```

Until Pages is enabled you may see a 404 page; follow the Deployment instructions below to publish the site.

## Customisation

- **Styling:** Edit `style.css` to change colours, fonts or layout.
- **Categories:** Modify the `<option>` elements in the form’s `select` element to add your own categories.
- **Persistence:** Replace localStorage with a server API or database if you need multi‑device synchronisation.

## Limitations

Because the app uses localStorage, your data will only be available in the browser where you entered it.  Clearing your browser’s storage will remove all expense data.

## License

Released under the MIT license.  See `LICENSE` for more information.