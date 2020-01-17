# incomplete_input_dict['cable_specs_pd'] = pd.read_excel(input_xlsx, 'cable_specs')
            # incomplete_input_dict['crew'] = incomplete_input_dict['project_data']['crew']
            # incomplete_input_dict['crew_cost'] = incomplete_input_dict['project_data']['crew_price']

            # #read in RSMeans per diem:
            # crew_cost = incomplete_input_dict['project_data']['crew_price']
            # crew_cost = crew_cost.set_index("Labor type ID", drop=False)
            # incomplete_input_dict['rsmeans_per_diem'] = crew_cost.loc['RSMeans', 'Per diem USD per day']
            # incomplete_input_dict['cable_specs_pd'] = pd.read_excel(input_xlsx, 'cable_specs')

            # #Read development tab:
            # incomplete_input_dict['development_df'] = pd.read_excel(input_xlsx, 'development')

            # Set the list and dictionary values on the master dictionary
            # self.default_input_dict['season_construct'] = ['spring', 'summer', 'fall']
            # self.default_input_dict['time_construct'] = 'normal'
            # self.default_input_dict['hour_day'] = {'long': 24, 'normal': 10}
            # self.default_input_dict['operational_construction_time'] = self.default_input_dict['hour_day'][
            #     self.default_input_dict['time_construct']]

# Warning: Silencing a NumPy warning that crashes the code.
import warnings
warnings.filterwarnings('ignore', 'numpy.ufunc size changed')
warnings.filterwarnings('ignore', 'This method will be removed in future versions.')

import os
import pandas as pd

class DataframeCache:
    """
    The DataframeCache class has only class methods and should not
    be instantiated. It's goal is to keep a central collection of
    dataframes read from Excel spreadsheets. These spreadsheets
    are cached so that they are not read from the filesystem every
    time they are accessed.

    The _cache variable holds a dictionary. Each dictionary is a tab
    of the project data spreadsheet

    This class looks for the LANDBOSSE_PROJECT_DATA_DIR environment
    variable to find the base filenames. If it doesn't find that
    environment variable, it defaults the folder to "project_data"
    """

    _cache = dict()

    @classmethod
    def read_xlsx_sheet(cls, xlsx_name, sheet_name):
        """
        Parameters
        ----------
        xlsx_name : str
            The base filename within the project data directory.
            Do not include .xlsx at the end of the filename.

        sheet_name : str
            The name of the tab to read.

        Returns
        -------
        pandas.DataFrame
            The dataframe as read from the tab.
        """
        xlsx_dir = os.environ.get('LANDBOSSE_PROJECT_DATA_DIR', 'landbosse_project_data')
        xlsx_path = os.path.join(xlsx_dir, f'{xlsx_name}.xlsx')

        # If the tab has already been read, just return the DataFrame
        if xlsx_name in cls._cache and sheet_name in cls._cache[xlsx_name]:
            return cls._cache[xlsx_name][sheet_name]

        # If the tab has not been read, but the .xlsx has had other reads
        elif xlsx_name in cls._cache:
            xlsx = cls._cache[xlsx_name]
            xlsx[sheet_name] = pd.read_excel(xlsx_path, sheet_name=sheet_name)
            return xlsx[sheet_name]

        # If the spreadsheet has never been read
        else:
            xlsx_dict = dict()
            sheet = pd.read_excel(xlsx_path, sheet_name=sheet_name)
            xlsx_dict[sheet_name] = sheet
            cls._cache[xlsx_name] = xlsx_dict
            return sheet
