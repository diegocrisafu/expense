# Expense Tracker App

This project is a lightweight **React** application for tracking personal expenses in the browser.  It allows you to add expenses with a date, description, category and amount, filter them by category and calculate the total.  Data is stored locally using the Web Storage API (`localStorage`), so your entries persist across page reloads.

## Features

- Add new expenses through a simple form
- Define categories (General, Food, Transport, Entertainment, Utilities, Health)
- Filter the list of expenses by category
- See a running total of the displayed expenses
- Data persists in your browser’s localStorage

## Getting Started

Open `index.html` in any modern browser to start using the app.  There is no build process since the page loads React and Babel from CDNs and compiles the JSX in the browser.

## Customisation

- **Styling:** Edit `style.css` to change colours, fonts or layout.
- **Categories:** Modify the `<option>` elements in the form’s `select` element to add your own categories.
- **Persistence:** Replace localStorage with a server API or database if you need multi‑device synchronisation.

## Limitations

Because the app uses localStorage, your data will only be available in the browser where you entered it.  Clearing your browser’s storage will remove all expense data.

## License

Released under the MIT license.  See `LICENSE` for more information.