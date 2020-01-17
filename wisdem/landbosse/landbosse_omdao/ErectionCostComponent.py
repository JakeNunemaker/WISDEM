from .LandBOSSEBaseComponent import LandBOSSEBaseComponent
from wisdem.landbosse.model import ErectionCost


class ErectionCostComopnent(LandBOSSEBaseComponent):
    """
    This class is an OpenMDAO wrapper around the ErectionCost component.
    """

    def setup(self):
        # Inputs, continuous
        self.add_input('num_turbines', val=1)
        self.add_input('hub_height_meters', val=1.0, units='m')
        self.add_input('rotor_diameter_m', val=1.0, units='m')
        self.add_input('overtime_multiplier', val=1.0)
        self.add_input('fuel_cost_usd_per_gal', val=1.0)
        self.add_input('construct_duration', val=9)
        self.add_input('wind_shear_exponent', val=0.2)
        self.add_input('turbine_rating_MW', val=1.5, units='MW')

        self.add_input('turbine_spacing_rotor_diameters', val=1.0)

        self.add_input('operational_construction_time',
                       val=1.0,
                       units='h')

        self.add_input('breakpoint_between_base_and_topping_percent',
                        val=70)

        self.add_input('rate_of_deliveries', val=10)

        # Inputs, discrete, dataframes
        self.add_discrete_input('components', val=None)
        self.add_discrete_input('crane_specs', val=None)
        self.add_discrete_input('weather_window', val=None)
        self.add_discrete_input('crew', val=None)
        self.add_discrete_input('crew_price', val=None)
        self.add_discrete_input('equip', val=None)
        self.add_discrete_input('equip_price', val=None)

        # Inputs, discrete, non dataframes
        self.add_discrete_input('allow_same_flag', val=False)
        self.add_discrete_input('hour_day', val={'long': 24, 'normal': 10})
        self.add_discrete_input('time_construct', val='normal')

        # Outputs, continuous
        # self.add_output('erection_wind_mult', val=1.0, desc='Wind multiplier for erection operations')
        # self.add_output('total_cost_summed_erection', val=1.0, desc='Sum of all erection costs')

        # Outputs, discrete, non dataframes
        self.add_discrete_output('erection_module_type_operation', val=[])

        self.add_discrete_output('erection_cost_details', val=[])

        # discrete_outputs['erection_cost_details'] = master_outputs_dict['erection_cost_csv']
        # discrete_outputs['erection_module_type_operation'] = master_outputs_dict['erection_module_type_operation']

        # Outputs, discrete, dataframes
        self.add_discrete_output('total_erection_cost', val=None)
        self.add_discrete_output('crane_data_output', val=None)
        self.add_discrete_output('crane_cost_details', val=None)
        self.add_discrete_output('management_crews_cost', val=None)

        self.add_discrete_output('management_crews_cost_grouped',
                                 val=None)

        self.add_discrete_output('component_name_topvbase',
                                 val=None)

        self.add_discrete_output('possible_cranes',
                                 val=None)

        self.add_discrete_output('crane_specs_with_offload',
                                 val=None)

        self.add_discrete_output('separate_topbase_crane_cost',
                                 val=None)

        self.add_discrete_output('topbase_same_crane_cost',
                                 val=None)

        self.add_discrete_output('crane_choice',
                                 val=None)

    def compute(self, inputs, outputs, discrete_inputs=None, discrete_outputs=None):
        """
        This runs the ErectionCost module using the inputs and outputs into and
        out of this module.

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

        # Breakpoint between base and topping is called a percentage but
        # it should be between 0 and 1. Make that conversion here.
        x = inputs['breakpoint_between_base_and_topping_percent'] / 100
        master_inputs_dict['breakpoint_between_base_and_topping_percent'] = x

        # Put the project data DataFrames where they should go.
        project_data = dict()
        project_data['components'] = discrete_inputs['components']
        project_data['crane_specs'] = discrete_inputs['crane_specs']
        project_data['weather_window'] = discrete_inputs['weather_window']
        project_data['crew'] = discrete_inputs['crew']
        project_data['crew_price'] = discrete_inputs['crew_price']
        project_data['equip'] = discrete_inputs['equip']
        project_data['equip_price'] = discrete_inputs['equip_price']
        master_inputs_dict['project_data'] = project_data

        # Run the module
        module = ErectionCost(master_inputs_dict, master_outputs_dict, 'WISDEM')
        module.run_module()

        # Copy the numeric outputs into the outputs object
        for key in outputs.keys():
            outputs[key] = master_outputs_dict[key]

        # Copy the discrete outputs into their object
        discrete_outputs['erection_cost_details'] = master_outputs_dict['erection_cost_csv']
        discrete_outputs['erection_module_type_operation'] = master_outputs_dict['erection_module_type_operation']

        # Log the outputs if needed
        if self.options['verbosity']:
            self.print_verbose_module_type_operation(type(self).__name__,
                                                     master_outputs_dict['erection_module_type_operation'])
            self.print_verbose_details(type(self).__name__,
                                       master_outputs_dict['erection_cost_csv'])
