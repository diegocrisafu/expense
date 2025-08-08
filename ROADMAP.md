# Roadmap – Expense Tracker

This roadmap captures the next major work streams for the expense
tracker, based on the Value Plan.

1. **Editing functionality** – Replace the static expense list with
   editable rows or a modal dialog that allows the user to modify
   date, description, category and amount.  Persist edits to
   `localStorage`.

2. **Data import/export** – Add buttons to export the current
   expenses to JSON and CSV formats and to import a file back into
   the app.  Provide clear user feedback when importing/exporting.

3. **Search and filtering enhancements** – Implement a text search
   input that filters the list by description and date.  Combine this
   with the existing category filter for advanced querying.

4. **Dark mode support** – Introduce CSS variables for colours and
   implement a toggle that switches between light and dark themes.

5. **Test suite and CI** – Set up Jest and React Testing Library to
   cover the main components (form, list, chart).  Add a GitHub
   Actions workflow to run tests on every push and pull request.