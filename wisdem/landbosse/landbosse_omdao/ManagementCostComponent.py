import openmdao.api as om
from wisdem.landbosse.model import ManagementCost
from .LandBOSSEBaseComponent import LandBOSSEBaseComponent


class ManagementCostComponent(LandBOSSEBaseComponent):
    """
    This is an OpenMDAO wrapper around the ManagementCost component
    """

    def initialize(self):
        """
        There is one option for this component: verbosity. If verbosity is
        True, then the component prints the costs to standard output
        when it calculates them
        """
        self.options.declare('verbosity', default=True)

    def setup(self):
        # Inputs
        self.add_discrete_input('site_facility_building_area_df',
                                val=None,
                                desc='pd.DataFrame: Site facility building area.')
        self.add_input('turbine_rating_MW', val=1.0, units='MW', desc='Turbine rating in MW')
        self.add_input('project_value_usd', val=1.0, units='USD', desc='Project value, USD')
        self.add_input('foundation_cost_usd', val=1.0, units='USD', desc='Foundation cost')
        self.add_input('construct_duration', val=1.0, desc='Construct duration in months')
        self.add_input('num_hwy_permits', val=1.0, desc='Number of highway permits')
        self.add_input('num_turbines', val=1.0, desc='Number of turbines in project')
        self.add_input('project_size_megawatts', units='MW', desc='Project size in megawatts')
        self.add_input('hub_height_meters', val=1.0, units='m', desc='Hub height in meters')
        self.add_input('num_access_roads', val=1.0, desc='Number of access roads')
        self.add_input('markup_contingency', val=1.0, desc='Markup contingency')
        self.add_input('markup_warranty_management', val=1.0, desc='Markup for warranty management')
        self.add_input('markup_profit_margin', val=1.0, desc='Markup for warranty management')
        self.add_input('markup_sales_and_use_tax', val=1.0, desc='Markup for sales and use tax')
        self.add_input('markup_overhead', val=1.0, desc='markup_profit_margin')

        # Outputs
        self.add_output('insurance_usd', units='USD', desc='Insurance cost', val=1.0)
        self.add_output('construction_permitting_usd', units='USD', desc='Construction permitting', val=1.0)
        self.add_output('project_management_usd', units='USD', desc='Project management', val=1.0)
        self.add_output('bonding_usd', units='USD', desc='bonding', val=1.0)
        self.add_output('site_facility_usd', units='USD', val=1.0)
        self.add_output('total_management_cost', units='USD', val=1.0)
        self.add_output('markup_contingency_usd', units='USD', val=1.0)
        self.add_output('engineering_usd', units='USD', val=1.0)
        self.add_discrete_output('management_cost_details', val=None, desc='Management cost details')
        self.add_discrete_output('management_module_type_operation', val=None, desc='Itemization of management costs')

    def compute(self, inputs, outputs, discrete_inputs=None, discrete_outputs=None):
        """
        Note: inputs, discrete_inputs are not dictionaries. They do support
        [] notation. inputs is of class 'openmdao.vectors.default_vector.DefaultVector'
        discrete_inputs is of class openmdao.core.component._DictValues. Other than
        [] brackets, they do not behave like dictionaries. See the following
        documentation for details.

        http://openmdao.org/twodocs/versions/latest/_srcdocs/packages/vectors/default_vector.html
        https://mdolab.github.io/OpenAeroStruct/_modules/openmdao/core/component.html

        Parameters
        ----------
        inputs : openmdao.vectors.default_vector.DefaultVector
            A dictionary-like object with NumPy arrays that hold float
            inputs. Note that since these are NumPy arrays, they
            need indexing to pull out simple float64 values.

        outputs : openmdao.vectors.default_vector.DefaultVector
            A dictionary-like object to store outputs.

        discrete_inputs : openmdao.core.component._DictValues
            A dictionary-like with the non-numeric inputs (like
            pandas.DataFrame)

        discrete_outputs : openmdao.core.component._DictValues
            A dictionary-like for non-numeric outputs (like
            pandas.DataFrame)
        """
        # Create real dictionaries to pass to the module
        inputs_dict = {key: inputs[key][0] for key in inputs.keys()}
        discrete_inputs_dict = {key: value for key, value in discrete_inputs.items()}
        master_inputs_dict = {**inputs_dict, **discrete_inputs_dict}
        master_outputs_dict = dict()
        module = ManagementCost(master_inputs_dict, master_outputs_dict, 'WISDEM')
        module.run_module()

        # Copy the numeric outputs into the outputs object
        for key in outputs.keys():
            outputs[key] = master_outputs_dict[key]

        # Copy the discrete outputs into their object
        discrete_outputs['management_cost_details'] = master_outputs_dict['management_cost_csv']
        discrete_outputs['management_module_type_operation'] = master_outputs_dict['mangement_module_type_operation']

        # Log the outputs if needed
        if self.options['verbosity']:
            self.print_verbose_module_type_operation('ManagementCost',
                                                     master_outputs_dict['mangement_module_type_operation'])
